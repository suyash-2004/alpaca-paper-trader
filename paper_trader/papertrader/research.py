"""Compare a SMALL, pre-specified set of strategy variants. Not a parameter search.

Honest-use rules: choose variants from published/structural reasoning BEFORE looking at results,
judge on development + walk-forward only, and treat the original holdout as already seen
(confirmatory only). Only paper-forward trading can now provide untouched evidence.
Usage: python -m papertrader.research"""
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from .backtest import run_backtest, summarize
from .config import load_config
from .guards import load_credentials
from .history import fetch_history, trading_days

BASE = Path(__file__).resolve().parent.parent

VARIANTS = {
    "V0 baseline (or15, 1.5R)": dict(or_minutes=15, target_r=1.5),
    "V1 +rel-volume>=1.5": dict(or_minutes=15, target_r=1.5, min_rel_volume=1.5),
    "V2 V1 +SPY>open": dict(or_minutes=15, target_r=1.5, min_rel_volume=1.5, market_filter=True),
    "V3 ATR stop, hold to close, rel-vol>=1.5": dict(or_minutes=15, target_r=0, min_rel_volume=1.5, stop_mode="atr", min_risk_pct=0.1),
    "V4 V3 +SPY>open": dict(or_minutes=15, target_r=0, min_rel_volume=1.5, stop_mode="atr", min_risk_pct=0.1, market_filter=True),
    "V5 range-mid stop, hold to close, rel-vol>=1.5": dict(or_minutes=15, target_r=0, min_rel_volume=1.5),
}


def main():
    from .alpaca_adapter import AlpacaIexData
    cfg = load_config(BASE / "config.json")
    ev = cfg.evaluation
    d = lambda s: date.fromisoformat(s)  # noqa: E731
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    bars = fetch_history(AlpacaIexData(key, secret), sorted(set(cfg.universe) | {"SPY"}), d(ev.data_start), d(ev.holdout_end), BASE / "state" / "cache")
    days = trading_days({"SPY": bars["SPY"]}, d(ev.data_start), d(ev.holdout_end))
    periods = {"dev": [x for x in days if x <= d(ev.dev_end)],
               "walk-forward": [x for x in days if d(ev.dev_end) < x <= d(ev.walk_forward_end)],
               "holdout (SEEN)": [x for x in days if x > d(ev.walk_forward_end)]}
    out = {}
    for name, kw in VARIANTS.items():
        p = replace(cfg.strategy, **kw)
        out[name] = {}
        for pname, pdays in periods.items():
            s = summarize(run_backtest(bars, pdays, cfg, p, ev.seed), cfg.capital_usd)
            out[name][pname] = {k: s[k] for k in ("trades", "win_rate_pct", "gross_pnl", "estimated_costs", "net_pnl", "max_drawdown_pct", "expectancy_per_trade")}
        print(name)
        for pname, r in out[name].items():
            print("   %-15s trades=%4d win=%5.1f%% gross=%8.2f costs=%8.2f NET=%8.2f exp/trade=%6.2f maxDD=%5.1f%%" % (
                pname, r["trades"], r["win_rate_pct"], r["gross_pnl"], r["estimated_costs"], r["net_pnl"], r["expectancy_per_trade"], r["max_drawdown_pct"]), flush=True)
    (BASE / "reports").mkdir(exist_ok=True)
    (BASE / "reports" / "variants.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
