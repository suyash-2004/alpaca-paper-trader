"""python -m papertrader.research_daily : evaluate the three pre-registered daily strategies."""
import json
from datetime import date
from pathlib import Path

from .config import load_config
from .daily import Data, SECTORS, buy_hold, rsi2_reversion, sector_momentum, simulate, stats, trend_200
from .guards import load_credentials

BASE = Path(__file__).resolve().parent.parent


def load_bars(start=date(2015, 1, 1), end=date(2026, 10, 29)):
    cache = BASE / "state" / "cache" / "daily.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
        return {s: [(date.fromisoformat(r[0]), *r[1:]) for r in rows] for s, rows in raw.items()}
    from .alpaca_adapter import AlpacaIexData
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    bars = AlpacaIexData(key, secret).get_daily_bars(["SPY"] + SECTORS, start, end)
    cache.write_text(json.dumps({s: [[str(r[0]), *r[1:]] for r in rows] for s, rows in bars.items()}))
    return bars


def main():
    cfg = load_config(BASE / "config.json")
    cap = cfg.capital_usd
    bars = load_bars()
    print({s: (len(r), str(r[0][0]) if r else None) for s, r in bars.items()})
    data = Data(bars)
    first = next(i for i, d in enumerate(data.dates) if d >= date(2017, 1, 3))
    n = len(data.dates)
    strategies = {"SPY buy&hold (benchmark)": buy_hold(data), "S1 trend: SPY > 200d SMA": trend_200(data),
                  "S2 RSI(2)<10 above 200d, exit >5d SMA": rsi2_reversion(data), "S3 sector 12-1 momentum top3, monthly": sector_momentum(data)}
    windows = [("2017-2026", first, n), ("2017-2019", first, next(i for i, d in enumerate(data.dates) if d >= date(2020, 1, 2))),
               ("2020-2022", next(i for i, d in enumerate(data.dates) if d >= date(2020, 1, 2)), next(i for i, d in enumerate(data.dates) if d >= date(2023, 1, 3))),
               ("2023-2026", next(i for i, d in enumerate(data.dates) if d >= date(2023, 1, 3)), n)]
    for name, sig in strategies.items():
        res = simulate(data, sig, cap, first, n)
        print(name, "| trades", res["trades"], "| avg exposure %.0f%%" % res["exposure_pct"])
        for wname, lo, hi in windows:
            print("   %-10s %s" % (wname, stats(res["equity"], cap, lo - first, hi - first)))


if __name__ == "__main__":
    main()
