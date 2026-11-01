"""python -m papertrader.research_overnight [--holdout]

Long-only, multi-day (overnight) holding. PRE-REGISTERED 2026-10-29, before any result on this data
(do not edit after looking):
  data      : Alpaca consolidated (SIP) daily bars from 2016, split-adjusted only. No dividends, as in paper.
  universe  : index ETFs SPY, QQQ, IWM, DIA (no survivorship bias); the 11 SPDR sector ETFs for D3 only.
  B0 control: buy and hold SPY.
  D1        : hold SPY while its close > 200-day SMA, else cash.                        (daily.trend_200)
  D2        : buy SPY when close > 200d SMA and RSI(2) < 10; sell when close > 5d SMA.   (daily.rsi2_reversion)
  D3        : top 3 sectors by 12-1 month momentum (positive only), rebalanced monthly. (daily.sector_momentum)
  S1        : the day-trader skill's daily scanner (scan.find_setups, imported unchanged) on SPY, QQQ, IWM,
              DIA: enter on breakout_20d or reclaim_ma20, exit when the close is below the skill's ma20.
              1/3 weight each, at most 3 held (priority SPY, QQQ, IWM, DIA).
  execution : signal at close t, trade at the open of t+1; whole shares; settled cash only (T+1);
              target = weight x min(equity, max_account_exposure_usd); each day's buy <= max_order_notional_usd.
  costs     : REALISTIC 2 bps/side + SEC fee + FINRA TAF on sells; also the runner's conservative bps/side.
  periods   : each starts fresh with capital_usd. dev 2017-2020, validation 2021-2023,
              HOLDOUT 2024-01-01..2026-10-28 (opened once with --holdout; selected candidate and B0 only).
  pass      : stage 1: net > 0 AND max drawdown < B0's, in dev AND validation (realistic costs).
              select ONE: the stage-1 passer with the best validation Sharpe.
              stage 2: the same two tests on the holdout. A failure anywhere -> nothing is deployed.
  disclosure: D1-D3 were run once before on corrupt IEX daily data (void; rules unchanged since).
              S1 has never been run on daily data.

RESULT (2026-10-29): stage 1 passed D1, D2, D3, S1; D2 selected (validation Sharpe 0.62); holdout PASS.

DEPLOYMENT CHECK (--deployed; added AFTER the holdout was opened, so every period here is already seen):
  the runner sends ONE bracket-style order per position, so D2 as deployed holds at most
  max_order_notional_usd, with a broker-side GTC catastrophe stop 7% below cost (picked before running
  as ~3 sigma of a 5-day SPY move, not tuned) so the per-trade loss limit is enforceable overnight.
  RESULT: dev 2017-2020 net -$178 (realistic) / -$245 (conservative) -> FAILS. Diagnostic only: $4k with no
  stop +$70, $8k with the stop -$13. The stop (Feb-Mar 2020) removes D2's thin dev edge; without a stop the
  per-trade loss limit cannot be enforced overnight. NOT DEPLOYED. No further stop/size variants were tried."""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Costs, load_config
from .daily import SECTORS, Data, buy_hold, rsi2_reversion, sector_momentum, simulate, stats, trend_200
from .guards import load_credentials
from .research_intraday import skill_scan
from .timeutil import ET, UTC

BASE = Path(__file__).resolve().parent.parent
ETFS = ["SPY", "QQQ", "IWM", "DIA"]
ENTRY_TAGS = {"breakout_20d", "reclaim_ma20"}
REALISTIC = Costs(half_spread_bps=1.0, slippage_bps=1.0)
PERIODS = {"dev 2017-2020": (date(2017, 1, 1), date(2020, 12, 31)),
           "validation 2021-2023": (date(2021, 1, 1), date(2023, 12, 31)),
           "HOLDOUT 2024-2026": (date(2024, 1, 1), date(2026, 10, 28))}


def load_daily(symbols):
    f = BASE / "state" / "cache_sip" / "daily_split.json"
    if f.exists():
        raw = json.loads(f.read_text())
        return {s: [(date.fromisoformat(r[0]), *r[1:]) for r in rows] for s, rows in raw.items()}
    from .alpaca_adapter import AlpacaSipResearchData
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    got = AlpacaSipResearchData(key, secret).get_daily_bars(
        symbols, datetime(2016, 1, 1, tzinfo=UTC), datetime.now(UTC) - timedelta(minutes=20))
    today = datetime.now(ET).date()
    bars = {s: [r for r in rows if r[0] < today] for s, rows in got.items()}  # no partial session
    f.write_text(json.dumps({s: [[str(r[0]), *r[1:]] for r in rows] for s, rows in bars.items()}))
    return bars


def skill_swing(data, scan):
    syms = [s for s in ETFS if s in data.close]
    held = []

    def sig(i):
        view = {}
        for s in syms:
            c = data.close[s][i - 30:i + 1] if i >= 30 else []
            if len(c) == 31 and all(c):
                view[s] = scan.find_setups(c)
        for s in list(held):
            if s in view and data.close[s][i] < view[s][1]["ma20"]:
                held.remove(s)
        for s in syms:
            if s not in held and len(held) < 3 and s in view and ENTRY_TAGS & set(view[s][0]):
                held.append(s)
        return {s: 1.0 / 3 for s in sorted(held, key=syms.index)}
    return sig


def check(s, b0):
    return s["net_usd"] > 0 and s["max_dd_pct"] < b0["max_dd_pct"]


CATASTROPHE_STOP_PCT = 7.0


def deployed():
    cfg = load_config(BASE / "config.json")
    cap, cap_order = float(cfg.capital_usd), cfg.max_order_notional_usd
    data = Data(load_daily(ETFS + SECTORS))
    for cname, costs in (("realistic", REALISTIC), ("conservative", cfg.costs)):
        bps = costs.half_spread_bps + costs.slippage_bps
        for pname, (lo, hi) in PERIODS.items():
            a = next(i for i, d in enumerate(data.dates) if d >= lo)
            b = next((i for i, d in enumerate(data.dates) if d > hi), len(data.dates))
            for name, sig, kw in (("B0 buy&hold SPY", buy_hold, {}), ("D2 as tested", rsi2_reversion, {}),
                                  ("D2 as deployed", rsi2_reversion, {"exp": cap_order, "stop_pct": CATASTROPHE_STOP_PCT})):
                r = simulate(data, sig(data), cap, a, b, bps, kw.get("exp", cfg.max_account_exposure_usd), cap_order, costs,
                             kw.get("stop_pct"))
                s = {**stats([cap] + r["equity"], cap), "trades": r["trades"], "exposure_pct": round(r["exposure_pct"])}
                print("%-12s %-21s %-16s %s" % (cname, pname, name, s), flush=True)


def hold_check():
    """HOLD rule, pre-registered 2026-10-29 BEFORE this run: buy SPY at the open until max_account_exposure_usd is
    invested (each day's buy <= max_order_notional_usd, settled cash only), then hold. NO stop and NO automatic
    sale. Alert when the unrealized loss from its high-water mark reaches 20% of capital. 2017-01..2026-10 is
    all SEEN data: this only shows the path and how often the alert would have fired. It proves nothing."""
    cfg = load_config(BASE / "config.json")
    cap = float(cfg.capital_usd)
    data = Data(load_daily(ETFS + SECTORS))
    a = next(i for i, d in enumerate(data.dates) if d >= date(2017, 1, 1))
    spy = [data.close["SPY"][i] for i in range(a, len(data.dates))]
    for cname, costs in (("realistic", REALISTIC), ("conservative", cfg.costs)):
        r = simulate(data, buy_hold(data), cap, a, len(data.dates), costs.half_spread_bps + costs.slippage_bps,
                     cfg.max_account_exposure_usd, cfg.max_order_notional_usd, costs)
        eq = [cap] + r["equity"]
        print(cname, stats(eq, cap), "| SPY close-to-close %+.1f%% (never fully invested, so P&L differs)" % ((spy[-1] / spy[0] - 1) * 100))
    hwm, armed, alerts = 0.0, True, []
    for k, e in enumerate(eq):
        pnl = e - cap
        hwm = max(hwm, pnl)
        if armed and hwm - pnl >= 0.20 * cap:
            armed = False
            alerts.append((str(data.dates[a + k - 1]) if k else "start", round(pnl)))
        elif not armed and pnl >= hwm:
            armed = True
    print("alerts (date, P&L at alert) - conservative costs:", alerts)
    yr = {}
    for k in range(1, len(eq)):
        yr.setdefault(data.dates[a + k - 1].year, []).append(eq[k] - eq[k - 1])
    print("P&L by calendar year:", {y: round(sum(v)) for y, v in yr.items()})


def main(open_holdout=False):
    cfg = load_config(BASE / "config.json")
    cap = float(cfg.capital_usd)
    data = Data(load_daily(ETFS + SECTORS))
    print({s: (len([x for x in v if x]), str(data.dates[next(i for i, x in enumerate(v) if x)])) for s, v in data.close.items()}, flush=True)
    scan = skill_scan()
    make = {"B0 buy&hold SPY (control)": lambda: buy_hold(data), "D1 SPY > 200d SMA": lambda: trend_200(data),
            "D2 RSI(2) pullback": lambda: rsi2_reversion(data), "D3 sector 12-1 momentum": lambda: sector_momentum(data),
            "S1 skill scanner swing": lambda: skill_swing(data, scan)}
    conservative = cfg.costs.half_spread_bps + cfg.costs.slippage_bps

    def run(name, lo, hi, bps, costs):
        a = next(i for i, d in enumerate(data.dates) if d >= lo)
        b = next((i for i, d in enumerate(data.dates) if d > hi), len(data.dates))
        r = simulate(data, make[name](), cap, a, b, bps, cfg.max_account_exposure_usd, cfg.max_order_notional_usd, costs)
        s = stats([cap] + r["equity"], cap)
        return {**s, "trades": r["trades"], "exposure_pct": round(r["exposure_pct"])}

    out = {}
    real = REALISTIC.half_spread_bps + REALISTIC.slippage_bps
    stage = ["dev 2017-2020", "validation 2021-2023"]
    for cname, bps, costs in (("realistic", real, REALISTIC), ("conservative", conservative, cfg.costs)):
        for pname in stage:
            for name in make:
                out[(cname, pname, name)] = s = run(name, *PERIODS[pname], bps, costs)
                print("%-12s %-21s %-26s %s" % (cname, pname, name, s), flush=True)
    passers = [n for n in make if not n.startswith("B0") and all(
        check(out[("realistic", p, n)], out[("realistic", p, "B0 buy&hold SPY (control)")]) for p in stage)]
    print("\nstage 1 passers:", passers or "none", flush=True)
    chosen = max(passers, key=lambda n: out[("realistic", "validation 2021-2023", n)]["sharpe"]) if passers else None
    print("selected:", chosen, flush=True)
    if open_holdout and chosen:
        lock = BASE / "reports" / "overnight_holdout_lock.json"
        if lock.exists():
            sys.exit("holdout already opened: %s" % lock.read_text())
        lock.write_text(json.dumps({"opened": str(datetime.now(UTC)), "candidate": chosen}))
        for cname, bps, costs in (("realistic", real, REALISTIC), ("conservative", conservative, cfg.costs)):
            for name in ("B0 buy&hold SPY (control)", chosen):
                out[(cname, "HOLDOUT 2024-2026", name)] = s = run(name, *PERIODS["HOLDOUT 2024-2026"], bps, costs)
                print("%-12s %-21s %-26s %s" % (cname, "HOLDOUT 2024-2026", name, s), flush=True)
        ok = check(out[("realistic", "HOLDOUT 2024-2026", chosen)], out[("realistic", "HOLDOUT 2024-2026", "B0 buy&hold SPY (control)")])
        print("\nHOLDOUT:", "PASS" if ok else "FAIL", flush=True)
    (BASE / "reports").mkdir(exist_ok=True)
    name = "overnight_holdout.json" if open_holdout else "overnight_dev_val.json"
    (BASE / "reports" / name).write_text(json.dumps({" | ".join(k): v for k, v in out.items()}, indent=1))


if __name__ == "__main__":
    if "--hold" in sys.argv:
        hold_check()
    elif "--deployed" in sys.argv:
        deployed()
    else:
        main(open_holdout="--holdout" in sys.argv)
