"""Backtest of the exact same signal / levels / sizing code the runner uses, with costs.

Execution assumptions (all conservative where the 5-minute bars are ambiguous):
- signal is known at the CLOSE of the breakout bar; the entry limit order is evaluated on the NEXT
  bar's open (no look-ahead). Fill = open + half-spread + slippage; missed if that exceeds the limit.
- stop and target both inside one bar -> the STOP is assumed hit first.
- stops fill at the stop (or the bar open if it gapped below) minus extra stop slippage.
- target is a limit sell: fills at the target (or the open if it gapped above), no slippage.
- partial fills (10%), random rejections, SEC/TAF sell fees, and the T+1 settled-cash rule are modelled.
- positions are sold at the flat-by bar's open (market, with costs)."""
import math
import random
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean

from .config import Config, Costs, StrategyParams
from .journal import sell_fees
from .features import atr, market_up, rel_volume
from .strategy import Bar, Levels, Signal, build_levels, day_bars, find_signals, size_qty
from .timeutil import hhmm, to_et


@dataclass
class Path:
    symbol: str
    day: object
    entry_ts: object
    exit_ts: object
    entry_ref: float
    exit_ref: float
    entry_fill: float
    exit_fill: float
    reason: str
    frac: float
    levels: Levels


def simulate_path(sig: Signal, bars, lv: Levels, p: StrategyParams, c: Costs, rng):
    """Returns (Path, None) or (None, why-not)."""
    idx = next((i for i, b in enumerate(bars) if b.ts == sig.bar_end), None)
    if idx is None:
        return None, "no_entry_bar"
    if rng.random() < c.reject_prob:
        return None, "rejected"
    eb = bars[idx]
    fill = eb.o * (1 + (c.half_spread_bps + c.slippage_bps) / 1e4)
    if fill > lv.limit:
        return None, "missed_limit"
    if eb.o <= lv.stop or eb.o >= lv.target:
        return None, "gap_invalid"
    frac = rng.uniform(c.partial_fill_min, c.partial_fill_max) if rng.random() < c.partial_fill_prob else 1.0
    flat = hhmm(p.flat_by)
    sell_cost = (c.half_spread_bps + c.slippage_bps) / 1e4
    for j in range(idx, len(bars)):
        b = bars[j]
        if to_et(b.ts).time() >= flat:
            return Path(sig.symbol, sig.day, eb.ts, b.ts, eb.o, b.o, fill, b.o * (1 - sell_cost), "time", frac, lv), None
        if b.l <= lv.stop:  # stop checked first: conservative
            ref = lv.stop if j == idx else min(lv.stop, b.o)
            return Path(sig.symbol, sig.day, eb.ts, b.end, eb.o, ref, fill,
                        ref * (1 - (c.half_spread_bps + c.stop_slippage_bps) / 1e4), "stop", frac, lv), None
        if b.h >= lv.target:
            ref = lv.target if j == idx else max(lv.target, b.o)
            return Path(sig.symbol, sig.day, eb.ts, b.end, eb.o, ref, fill, ref, "target", frac, lv), None
    b = bars[-1]
    return Path(sig.symbol, sig.day, eb.ts, b.end, eb.o, b.c, fill, b.c * (1 - sell_cost), "data_end", frac, lv), None


_IDX = {}


def _by_day(bars):
    """{symbol: {day: [bars of that ET day]}}, built once per bars object (backtests rerun it many times)."""
    key = id(bars)
    if key not in _IDX or _IDX[key][0] is not bars:
        _IDX.clear()
        _IDX[key] = (bars, {s: _group(bs) for s, bs in bars.items()})
    return _IDX[key][1]


def _group(bs):
    out = {}
    for b in bs:
        out.setdefault(to_et(b.ts).date(), []).append(b)
    return out


def run_backtest(bars, days, cfg: Config, p: StrategyParams, seed=7, gate=None):
    """gate(symbol, day) -> bool: optional pre-trade filter (e.g. the skill's daily scanner setups)."""
    """bars: {symbol: [Bar]} for cfg.universe; days: ordered trading days to trade."""
    rng = random.Random(seed)
    c = cfg.costs
    capital = float(cfg.capital_usd)
    settled, pending, trades, equity = capital, [], [], []
    diag = {}
    idx = _by_day(bars)

    def bump(k):
        diag[k] = diag.get(k, 0) + 1

    for di, day in enumerate(days):
        settled += sum(a for d, a in pending if d <= day)
        pending = [(d, a) for d, a in pending if d > day]
        paths = []
        for sym, sig in [(s, g) for s in sorted(cfg.universe) for g in find_signals(s, idx.get(s, {}).get(day, []), day, p)]:
            if any(pa.symbol == sym and pa.exit_ts > sig.bar_end for pa in paths):
                bump("reentry_while_open")  # the live runner refuses a symbol it already holds
                continue
            bump("signals")
            if gate is not None and not gate(sym, day):
                bump("filter_gate")
                continue
            by_day = idx[sym]
            if p.min_rel_volume:
                rv = rel_volume(by_day, day, p.or_minutes)
                if rv is None or rv < p.min_rel_volume:
                    bump("filter_rel_volume")
                    continue
            if p.market_filter and not market_up(idx.get("SPY", {}), day, sig.bar_end - timedelta(minutes=5)):
                bump("filter_market")
                continue
            stop = sig.stop
            if p.stop_mode == "atr":
                a = atr(by_day, day)
                if a is None:
                    bump("filter_no_atr")
                    continue
                stop = sig.ref_close - p.atr_stop_frac * a
            lv = build_levels(sig.ref_close, stop, p)
            if lv is None:
                bump("levels_out_of_bounds")
                continue
            path, why = simulate_path(sig, day_bars(idx[sym][day], day), lv, p, c, rng)
            if path:
                paths.append(path)
            else:
                bump(why)
        accepted, spent = [], 0.0
        for pa in sorted(paths, key=lambda x: (x.entry_ts, x.symbol)):
            t = pa.entry_ts
            open_now = [a for a in accepted if a["exit_ts"] > t]
            realized = sum(a["net_pnl"] for a in accepted if a["exit_ts"] <= t)
            if len(open_now) >= cfg.max_open_positions:
                bump("risk_max_open")
                continue
            if -realized >= cfg.max_daily_loss_usd:
                bump("risk_daily_halt")
                continue
            open_cost = sum(a["entry_cost"] for a in open_now)
            room = min(capital - open_cost, cfg.max_account_exposure_usd - open_cost)
            qty = size_qty(pa.levels, cfg.max_loss_per_trade_usd, cfg.max_order_notional_usd, settled - spent, room)
            if qty < 1:
                bump("risk_size_zero")
                continue
            open_risk = sum((a["entry_fill"] - a["stop"]) * a["qty"] for a in open_now)
            if -realized + open_risk + qty * pa.levels.risk > cfg.max_daily_loss_usd:
                bump("risk_daily_worst_case")
                continue
            q = int(math.floor(qty * pa.frac))
            if q < 1:
                bump("partial_fill_zero")
                continue
            cost = q * pa.entry_fill
            proceeds = q * pa.exit_fill
            fees = sell_fees(q, pa.exit_fill, c)
            net = proceeds - fees - cost
            accepted.append({
                "symbol": pa.symbol, "day": str(day), "entry_ts": pa.entry_ts, "exit_ts": pa.exit_ts, "qty": q,
                "entry_fill": pa.entry_fill, "exit_fill": pa.exit_fill, "stop": pa.levels.stop, "reason": pa.reason,
                "gross_pnl": q * (pa.exit_ref - pa.entry_ref), "fees": fees, "net_pnl": net, "entry_cost": cost,
                "minutes": (pa.exit_ts - pa.entry_ts).total_seconds() / 60, "proceeds": proceeds - fees})
            spent += cost
        settled -= spent
        nxt = days[di + 1] if di + 1 < len(days) else None
        if accepted:
            pending.append((nxt or day + timedelta(days=1), sum(a["proceeds"] for a in accepted)))
        trades += accepted
        equity.append(settled + sum(a for _, a in pending))
    return {"trades": trades, "equity": equity, "days": [str(d) for d in days], "diagnostics": diag}


def _pct(vals, q):
    s = sorted(vals)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def summarize(result, capital, benchmark_bars=None, days=None):
    tr, eq = result["trades"], result["equity"]
    pnl = [t["net_pnl"] for t in tr]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x <= 0]
    peak, mdd, mdd_pct, e0 = capital, 0.0, 0.0, capital
    for e in eq:
        peak = max(peak, e)
        mdd, mdd_pct = max(mdd, peak - e), max(mdd_pct, (peak - e) / peak * 100 if peak else 0)
    n_days = len(result["days"]) or 1
    exposure = sum(t["entry_cost"] * t["minutes"] for t in tr) / (capital * 390 * n_days) * 100
    out = {
        "period": [result["days"][0], result["days"][-1]] if result["days"] else None, "trading_days": n_days,
        "trades": len(tr), "wins": len(wins), "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(tr) * 100, 1) if tr else 0.0,
        "gross_pnl": round(sum(t["gross_pnl"] for t in tr), 2), "net_pnl": round(sum(pnl), 2),
        "estimated_costs": round(sum(t["gross_pnl"] for t in tr) - sum(pnl), 2),
        "net_return_pct_of_capital": round(sum(pnl) / capital * 100, 2),
        "avg_win": round(mean(wins), 2) if wins else 0.0, "avg_loss": round(mean(losses), 2) if losses else 0.0,
        "expectancy_per_trade": round(mean(pnl), 2) if pnl else 0.0,
        "max_drawdown_usd": round(mdd, 2), "max_drawdown_pct": round(mdd_pct, 2),
        "avg_capital_exposure_pct": round(exposure, 2),
        "pnl_distribution": {k: round(_pct(pnl, q), 2) for k, q in
                             (("p05", .05), ("p25", .25), ("median", .5), ("p75", .75), ("p95", .95))},
        "worst_trade": round(min(pnl), 2) if pnl else 0.0, "best_trade": round(max(pnl), 2) if pnl else 0.0,
        "exit_reasons": {r: sum(1 for t in tr if t["reason"] == r) for r in sorted({t["reason"] for t in tr})},
        "diagnostics": result["diagnostics"],
    }
    if benchmark_bars and days:
        first, last = day_bars(benchmark_bars, days[0]), day_bars(benchmark_bars, days[-1])
        if first and last:
            r = last[-1].c / first[0].o - 1
            out["benchmark_spy_buy_and_hold"] = {"return_pct": round(r * 100, 2), "pnl_on_capital": round(r * capital, 2),
                                                 "note": "SPY open of first day to close of last day, no costs, fully invested"}
    return out
