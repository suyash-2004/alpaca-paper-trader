"""python -m papertrader.research_intraday
Skill-chain variants: the day-trader skill's DAILY scanner setups (scan.find_setups, imported unchanged)
gate the intraday ORB entry, exactly the scanner -> entry -> exit chain of the skill. Variants are fixed
below BEFORE results are seen. Consolidated SIP bars are used for research only (the live runner is IEX)."""
import importlib.util
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .backtest import run_backtest, summarize
from .config import load_config
from .guards import load_credentials
from .history import fetch_history, trading_days
from .strategy import day_bars

BASE = Path(__file__).resolve().parent.parent
SCAN = BASE.parent / ".claude" / "skills" / "trader-scanner" / "scan.py"
TREND = {"breakout_20d", "bull_flag", "reclaim_ma20"}
REVERT = {"oversold", "oversold_bounce"}


def skill_scan():
    spec = importlib.util.spec_from_file_location("skill_scan", SCAN)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def setup_tags(bars, scan):
    """{sym: {day: set(tags as of the PRIOR close)}} from daily closes built from the 5-min bars."""
    out = {}
    for s, bs in bars.items():
        closes = {}
        for d in sorted({b.ts.astimezone(__import__("papertrader.timeutil", fromlist=["ET"]).ET).date() for b in bs}):
            db = day_bars(bs, d)
            if db:
                closes[d] = db[-1].c
        ds = sorted(closes)
        out[s] = {}
        for i in range(31, len(ds)):
            tags, _ = scan.find_setups([closes[d] for d in ds[i - 31:i]])  # history through the prior close
            out[s][ds[i]] = set(tags)
    return out


def main():
    from .alpaca_adapter import AlpacaSipResearchData
    cfg = load_config(BASE / "config.json")
    ev, d = cfg.evaluation, date.fromisoformat
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    start = d(ev.data_start) - timedelta(days=75)  # 30+ sessions of warm-up for the scanner rules
    bars = fetch_history(AlpacaSipResearchData(key, secret), sorted(set(cfg.universe) | {"SPY"}), start, d(ev.holdout_end), BASE / "state" / "cache_sip")
    days = trading_days({"SPY": bars["SPY"]}, d(ev.data_start), d(ev.holdout_end))
    tags = setup_tags(bars, skill_scan())
    periods = {"dev": [x for x in days if x <= d(ev.dev_end)],
               "walk-forward": [x for x in days if d(ev.dev_end) < x <= d(ev.walk_forward_end)],
               "holdout (SEEN)": [x for x in days if x > d(ev.walk_forward_end)]}
    gate = lambda want: (lambda s, day: bool(tags.get(s, {}).get(day, set()) & want))  # noqa: E731
    variants = {
        "K0 plain ORB 15m/1.5R (SIP data)": (dict(or_minutes=15, target_r=1.5), None),
        "K1 ORB gated by skill trend setups": (dict(or_minutes=15, target_r=1.5), gate(TREND)),
        "K2 ORB gated by skill oversold setups": (dict(or_minutes=15, target_r=1.5), gate(REVERT)),
        "K3 K1 with skill-default 2R target": (dict(or_minutes=15, target_r=2.0), gate(TREND)),
    }
    for name, (kw, g) in variants.items():
        print(name)
        for pname, pdays in periods.items():
            s = summarize(run_backtest(bars, pdays, cfg, replace(cfg.strategy, **kw), ev.seed, gate=g), cfg.capital_usd)
            print("   %-15s trades=%4d win=%5.1f%% gross=%8.2f costs=%8.2f NET=%8.2f exp/trade=%6.2f maxDD=%5.1f%%" % (
                pname, s["trades"], s["win_rate_pct"], s["gross_pnl"], s["estimated_costs"], s["net_pnl"], s["expectancy_per_trade"], s["max_drawdown_pct"]), flush=True)


if __name__ == "__main__":
    main()
