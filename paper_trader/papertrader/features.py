"""Point-in-time features for signal filters. Every function uses only sessions BEFORE `day`
(plus today's bars up to the signal bar), so there is no look-ahead.

by_day: {date: [Bar]} regular-session 5-minute bars for one symbol."""
from datetime import timedelta
from statistics import mean

from .strategy import day_bars
from .timeutil import BAR_MIN, ET, session_open, to_et


def _prior(by_day, day, n):
    return [d for d in sorted(by_day) if d < day][-n:]


def _or_volume(bars, day, or_minutes):
    end = session_open(day) + timedelta(minutes=or_minutes)
    rng = [b for b in day_bars(bars, day) if to_et(b.ts) < end]
    return sum(b.v for b in rng) if len(rng) == or_minutes // BAR_MIN else None


def rel_volume(by_day, day, or_minutes, lookback=14):
    """Opening-range volume today / mean opening-range volume of the previous `lookback` sessions."""
    today = _or_volume(by_day.get(day, []), day, or_minutes)
    prior = [v for d in _prior(by_day, day, lookback) if (v := _or_volume(by_day[d], d, or_minutes))]
    if today is None or len(prior) < 10 or mean(prior) <= 0:
        return None
    return today / mean(prior)


def atr(by_day, day, lookback=14):
    """Mean true range of the previous `lookback` sessions (session high/low/close from 5-min bars)."""
    days = _prior(by_day, day, lookback + 1)
    ohlc = []
    for d in days:
        bs = day_bars(by_day[d], d)
        if bs:
            ohlc.append((max(b.h for b in bs), min(b.l for b in bs), bs[-1].c))
    if len(ohlc) < 11:
        return None
    trs = [max(h - l, abs(h - pc), abs(l - pc)) for (h, l, _), (_, _, pc) in zip(ohlc[1:], ohlc[:-1])]
    return mean(trs[-lookback:])


def market_up(spy_by_day, day, breakout_bar_start):
    """True if SPY's close at the breakout bar is above SPY's session open."""
    bs = day_bars(spy_by_day.get(day, []), day)
    cur = [b for b in bs if b.ts == breakout_bar_start]
    return bool(bs and cur and cur[0].c > bs[0].o)
