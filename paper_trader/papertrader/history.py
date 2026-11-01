"""Historical 5-minute IEX bars with a local month-by-month cache (state/cache, git-ignored)."""
import gzip
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .strategy import Bar
from .timeutil import ET, UTC


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        first = date(y, m, 1)
        nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
        yield first, nxt - timedelta(days=1)
        y, m = nxt.year, nxt.month


def fetch_history(data, symbols, start: date, end: date, cache_dir, chunk=20):
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    out = {s: [] for s in symbols}
    today = datetime.now(ET).date()
    for m0, m1 in _months(start, end):
        tag = m0.strftime("%Y-%m")
        complete = m1 < today  # only closed months are cached
        need = []
        for s in symbols:
            f = cache / ("%s_%s.json.gz" % (s, tag))
            if complete and f.exists():
                rows = json.loads(gzip.open(f, "rt").read())
                out[s] += [Bar(datetime.fromisoformat(r[0]), *r[1:]) for r in rows]
            else:
                need.append(s)
        for i in range(0, len(need), chunk):
            part = need[i:i + chunk]
            lo = datetime.combine(max(m0, start), datetime.min.time(), tzinfo=ET)
            hi = datetime.combine(min(m1, end) + timedelta(days=1), datetime.min.time(), tzinfo=ET)
            hi = min(hi, datetime.now(ET) - timedelta(minutes=20))  # SIP history must be >15 min old
            got = data.get_bars(part, lo.astimezone(UTC), hi.astimezone(UTC))
            for s in part:
                bs = got.get(s, [])
                out[s] += bs
                if complete:
                    with gzip.open(cache / ("%s_%s.json.gz" % (s, tag)), "wt") as fh:
                        json.dump([[b.ts.isoformat(), b.o, b.h, b.l, b.c, b.v] for b in bs], fh)
    return {s: sorted(v, key=lambda b: b.ts) for s, v in out.items()}


def trading_days(bars_by_symbol, start: date, end: date):
    """Days on which the benchmark (or any symbol) actually printed bars."""
    days = set()
    for bs in bars_by_symbol.values():
        days |= {b.ts.astimezone(ET).date() for b in bs}
    return sorted(d for d in days if start <= d <= end)
