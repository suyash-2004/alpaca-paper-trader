"""Separated evaluation: development -> walk-forward validation -> one-shot holdout.
Paper-forward results come from the runner's own trade journal (see report_forward).

- development   : data_start .. dev_end. The ONLY place parameters are chosen (small fixed grid).
- walk-forward  : dev_end .. walk_forward_end, in folds of wf_test_days. For each fold parameters
                  are re-selected using only days BEFORE that fold, then applied to the fold.
- holdout       : walk_forward_end .. holdout_end. Run once with parameters chosen from data before
                  it. A lock file records those parameters; a later run with different parameters
                  is refused, so the final period cannot be tuned on.
Nothing here proves future profitability."""
import hashlib
import itertools
import json
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path

from .backtest import run_backtest, summarize
from .config import Config, StrategyParams


class EvalError(RuntimeError):
    pass


def _d(s):
    return date.fromisoformat(s)


def grid(cfg: Config):
    g = cfg.evaluation.param_grid
    keys = sorted(g)
    return [replace(cfg.strategy, **dict(zip(keys, vals))) for vals in itertools.product(*(g[k] for k in keys))]


def select(bars, days, cfg: Config):
    """Pick the grid point with the best NET P&L on `days` (needs min_trades_to_select trades)."""
    table = []
    for p in grid(cfg):
        res = run_backtest(bars, days, cfg, p, cfg.evaluation.seed)
        s = summarize(res, cfg.capital_usd)
        table.append({"params": asdict(p), "trades": s["trades"], "net_pnl": s["net_pnl"], "max_drawdown_pct": s["max_drawdown_pct"]})
    ok = [(t["net_pnl"], i) for i, t in enumerate(table) if t["trades"] >= cfg.evaluation.min_trades_to_select]
    if not ok:
        return None, table
    return StrategyParams(**table[max(ok)[1]]["params"]), table


def split(days, cfg: Config):
    ev = cfg.evaluation
    miss = [k for k in ("data_start", "dev_end", "walk_forward_end", "holdout_end") if not getattr(ev, k)]
    if miss:
        raise EvalError("evaluation dates not set: %s" % miss)
    a, b, c, d = (_d(getattr(ev, k)) for k in ("data_start", "dev_end", "walk_forward_end", "holdout_end"))
    if not (a < b < c < d):
        raise EvalError("need data_start < dev_end < walk_forward_end < holdout_end")
    return ([x for x in days if a <= x <= b], [x for x in days if b < x <= c], [x for x in days if c < x <= d])


def evaluate(bars, benchmark_bars, days, cfg: Config, report_dir):
    dev, wf, hold = split(days, cfg)
    if min(len(dev), len(wf), len(hold)) < 5:
        raise EvalError("each period needs at least 5 trading days of data")
    out = {"assumptions": {"costs": asdict(cfg.costs), "seed": cfg.evaluation.seed, "capital_usd": cfg.capital_usd,
                           "limits": {k: getattr(cfg, k) for k in (
                               "max_loss_per_trade_usd", "max_daily_loss_usd", "max_open_positions",
                               "max_order_notional_usd", "max_account_exposure_usd")},
                           "fill_model": "see backtest.py docstring", "data": "Alpaca IEX 5-minute bars (not consolidated)"}}
    dev_p, table = select(bars, dev, cfg)
    out["development"] = {"grid": table, "selected": asdict(dev_p) if dev_p else None}
    if dev_p is None:
        out["verdict"] = "no parameter set reached min_trades_to_select in development: stay idle"
        return _save(out, report_dir)
    out["development"]["result"] = summarize(run_backtest(bars, dev, cfg, dev_p, cfg.evaluation.seed), cfg.capital_usd, benchmark_bars, dev)

    folds, n = [], cfg.evaluation.wf_test_days
    trades_all, eq_all = [], []
    for i in range(0, len(wf), n):
        test = wf[i:i + n]
        train = [d for d in days if d < test[0]]
        p, _ = select(bars, train, cfg)
        if p is None:
            folds.append({"test": [str(test[0]), str(test[-1])], "params": None, "note": "idle: no valid parameters"})
            continue
        res = run_backtest(bars, test, cfg, p, cfg.evaluation.seed)
        trades_all += res["trades"]
        folds.append({"test": [str(test[0]), str(test[-1])], "params": asdict(p), "result": summarize(res, cfg.capital_usd)})
    stitched = {"trades": trades_all, "equity": _stitch(folds, cfg.capital_usd), "days": [str(d) for d in wf], "diagnostics": {}}
    out["walk_forward"] = {"folds": folds, "stitched": summarize(stitched, cfg.capital_usd, benchmark_bars, wf)}

    final_p, _ = select(bars, [d for d in days if d <= wf[-1]], cfg)
    if final_p is None:
        out["holdout"] = {"note": "no valid parameters from data before the holdout: idle"}
    else:
        h = hashlib.sha256(json.dumps(asdict(final_p), sort_keys=True).encode()).hexdigest()
        lock = Path(report_dir) / "holdout_lock.json"
        if lock.exists() and json.loads(lock.read_text())["params_sha256"] != h:
            raise EvalError("holdout already evaluated with different parameters (%s). Re-tuning against it is "
                            "refused; delete the lock only if you accept the holdout is now burned." % lock)
        res = summarize(run_backtest(bars, hold, cfg, final_p, cfg.evaluation.seed), cfg.capital_usd, benchmark_bars, hold)
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"params_sha256": h, "params": asdict(final_p)}, indent=1))
        out["holdout"] = {"params": asdict(final_p), "result": res}
    return _save(out, report_dir)


def _stitch(folds, capital):
    eq, level = [], capital
    for f in folds:
        r = f.get("result")
        if not r:
            continue
        # fold equity restarts at capital; chain the fold P&L so the stitched drawdown is meaningful
        level += r["net_pnl"]
        eq.append(level)
    return eq


def _save(out, report_dir):
    d = Path(report_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "backtest_report.json").write_text(json.dumps(out, indent=1, default=str))
    return out


def report_forward(trades_jsonl, capital, costs_note="paper fills; regulatory fees estimated"):
    """Paper-forward summary straight from the runner's journal (no simulation)."""
    p = Path(trades_jsonl)
    rows = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
    days = sorted({r["day"] for r in rows})
    trades = [{"net_pnl": r["net_pnl"], "gross_pnl": r["gross_pnl"], "reason": r["exit_reason"], "entry_cost": r["entry_cost"],
               "minutes": 0.0} for r in rows]
    eq, level = [], capital
    for d in days:
        level += sum(r["net_pnl"] for r in rows if r["day"] == d)
        eq.append(level)
    s = summarize({"trades": trades, "equity": eq, "days": days, "diagnostics": {}}, capital)
    s["note"] = costs_note + "; exposure not tracked in the journal"
    s.pop("avg_capital_exposure_pct", None)
    return s
