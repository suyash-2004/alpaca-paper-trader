"""python -m papertrader.research_momentum

Intraday time-series momentum (Gao, Han, Li & Zhou, "Market intraday momentum", JFE 2018):
the first half-hour return (previous close -> 10:00) predicts the last half-hour return.

PRE-REGISTERED (written before any result was seen; do not edit after looking):
  universe : SPY, QQQ, IWM, DIA (priority in that order when slots are limited)
  r1       : close of the 9:55 bar (price at 10:00) / previous session's last regular close - 1
  r12      : close of the 15:25 bar / close of the 14:55 bar - 1   (15:00-15:30 half hour)
  M0 control: buy every day (no signal)       -> measures plain drift + costs
  M1       : buy if r1 > 0
  M2       : buy if r1 > 0 and r12 > 0
  entry    : open of the 15:30 bar (+ half-spread + slippage), marketable limit 10 bps above
  stop     : 1% below entry fill (broker-side stop leg); exit at the 15:55 bar open otherwise
  sizing   : cash-only, whole shares, each trade <= max_order_notional_usd, all trades <= max exposure,
             <= max_open_positions, per-trade loss at stop <= max_loss_per_trade_usd, sale proceeds T+1
  periods  : dev 2016-2020, validation 2021-2023, HOLDOUT 2024-01-01..2026-10-27 (opened once, --holdout)
  pass     : net P&L > 0 under REALISTIC ETF costs in dev AND validation AND holdout, and M1 or M2 beats M0.
Costs shown at two levels: the runner's conservative defaults and realistic ETF costs."""
import json
import sys
from datetime import date, time
from pathlib import Path

from .config import Costs, load_config
from .history import fetch_history
from .journal import sell_fees
from .strategy import day_bars
from .timeutil import to_et

BASE = Path(__file__).resolve().parent.parent
ETFS = ["SPY", "QQQ", "IWM", "DIA"]
STOP_PCT = 1.0
REALISTIC = Costs(half_spread_bps=0.5, slippage_bps=1.0, stop_slippage_bps=3.0, partial_fill_prob=0, reject_prob=0)
PERIODS = {"dev 2016-2020": (date(2016, 1, 1), date(2020, 12, 31)),
           "validation 2021-2023": (date(2021, 1, 1), date(2023, 12, 31)),
           "HOLDOUT 2024-2026": (date(2024, 1, 1), date(2026, 10, 27))}


def day_table(bars):
    """{day: {'r1','r12','entry_open','exit_open','path_lows'}} for complete regular sessions only."""
    by = {}
    for b in bars:
        by.setdefault(to_et(b.ts).date(), []).append(b)
    days = sorted(by)
    out, prev_close = {}, None
    for d in days:
        bs = day_bars(by[d], d)
        t = {to_et(b.ts).time(): b for b in bs}
        need = [time(9, 55), time(14, 55), time(15, 25), time(15, 30), time(15, 55)]
        if len(bs) == 78 and prev_close and all(k in t for k in need):
            out[d] = {"r1": t[time(9, 55)].c / prev_close - 1,
                      "r12": t[time(15, 25)].c / t[time(14, 55)].c - 1,
                      "entry_open": t[time(15, 30)].o, "exit_open": t[time(15, 55)].o,
                      "lows": [t[time(15, 30 + 5 * k)].l for k in range(5)]}
        prev_close = bs[-1].c if len(bs) == 78 else None  # half days / gaps break the chain
    return out


def simulate(tables, rule, cfg, c: Costs, lo, hi):
    cap = float(cfg.capital_usd)
    settled, pending, trades, eq = cap, 0.0, [], []
    days = sorted({d for t in tables.values() for d in t if lo <= d <= hi})
    for d in days:
        settled += pending  # yesterday's sale proceeds settle today (T+1)
        pending = 0.0
        picks = [s for s in ETFS if d in tables.get(s, {}) and rule(tables[s][d])][:cfg.max_open_positions]
        if not picks:
            eq.append(settled)
            continue
        per = min(cfg.max_order_notional_usd, cfg.max_account_exposure_usd / len(picks), settled / len(picks))
        day_loss_budget = cfg.max_daily_loss_usd
        for s in picks:
            r = tables[s][d]
            fill = r["entry_open"] * (1 + (c.half_spread_bps + c.slippage_bps) / 1e4)
            limit = r["entry_open"] * 1.001
            q = int(per / limit)
            stop = fill * (1 - STOP_PCT / 100)
            q = min(q, int(cfg.max_loss_per_trade_usd / (fill - stop)), int(day_loss_budget / (fill - stop)))
            if q < 1:
                continue
            day_loss_budget -= q * (fill - stop)
            lows = [x for x in r["lows"] if x is not None]
            if any(x <= stop for x in lows):
                ref, why = stop, "stop"
                px = ref * (1 - (c.half_spread_bps + c.stop_slippage_bps) / 1e4)
            else:
                ref, why = r["exit_open"], "time"
                px = ref * (1 - (c.half_spread_bps + c.slippage_bps) / 1e4)
            fees = sell_fees(q, px, c)
            settled -= q * fill
            pending += q * px - fees
            trades.append({"day": str(d), "sym": s, "gross": q * (ref - r["entry_open"]), "net": q * (px - fill) - fees, "why": why})
        eq.append(settled + pending)
    return trades, eq


def summary(trades, eq, cap):
    net = sum(t["net"] for t in trades)
    peak, mdd = cap, 0.0
    for e in eq:
        peak, mdd = max(peak, e), max(mdd, (peak - e) / peak)
    wins = [t for t in trades if t["net"] > 0]
    return {"trades": len(trades), "win%": round(100 * len(wins) / len(trades), 1) if trades else 0,
            "gross": round(sum(t["gross"] for t in trades), 2), "net": round(net, 2),
            "net/trade": round(net / len(trades), 3) if trades else 0, "maxDD%": round(100 * mdd, 2),
            "stops": sum(t["why"] == "stop" for t in trades)}


def main(open_holdout=False):
    from .alpaca_adapter import AlpacaSipResearchData
    from .guards import load_credentials
    cfg = load_config(BASE / "config.json")
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    bars = fetch_history(AlpacaSipResearchData(key, secret), ETFS, date(2016, 1, 1), date(2026, 10, 28), BASE / "state" / "cache_sip", chunk=4)
    tables = {s: day_table(bars[s]) for s in ETFS}
    rules = {"M0 control (every day)": lambda r: True, "M1 r1>0": lambda r: r["r1"] > 0,
             "M2 r1>0 and r12>0": lambda r: r["r1"] > 0 and r["r12"] > 0}
    out = {}
    for cname, costs in (("conservative costs", cfg.costs), ("realistic ETF costs", REALISTIC)):
        for pname, (lo, hi) in PERIODS.items():
            if pname.startswith("HOLDOUT") and not open_holdout:
                continue
            for rname, rule in rules.items():
                s = summary(*simulate(tables, rule, cfg, costs, lo, hi), cfg.capital_usd)
                out["%s | %s | %s" % (cname, pname, rname)] = s
                print("%-20s %-21s %-22s %s" % (cname, pname, rname, s), flush=True)
    (BASE / "reports").mkdir(exist_ok=True)
    name = "momentum_holdout.json" if open_holdout else "momentum_dev_val.json"
    (BASE / "reports" / name).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(open_holdout="--holdout" in sys.argv)
