"""Deterministic opening-range-breakout (ORB) long entry, adapted from the day-trader
skill (trader-scanner `orb`, trader-exit `or-mid` stop / `1R` target, flat-by time).

Mismatches with the skill, on purpose:
- the skill scans on Yahoo (unofficial, delayed); this uses Alpaca IEX bars, so IEX volume is a
  small slice of consolidated volume and thin symbols may have missing bars (we skip those).
- the skill sizes by a risk budget; sizing here is additionally capped by settled cash.
- a signal is only valid while it is the newest completed bar (no chasing old breakouts)."""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from .config import StrategyParams
from .timeutil import BAR_MIN, ET, hhmm, session_open, to_et


@dataclass(frozen=True)
class Bar:
    ts: datetime  # bar START, tz-aware
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

    @property
    def end(self):
        return self.ts + timedelta(minutes=BAR_MIN)


@dataclass(frozen=True)
class Signal:
    symbol: str
    day: object
    bar_end: datetime   # when the signal became known (close of the breakout bar)
    ref_close: float
    or_high: float
    or_low: float
    stop: float         # opening-range midpoint


@dataclass(frozen=True)
class Levels:
    limit: float
    stop: float
    target: float

    @property
    def risk(self):
        return self.limit - self.stop


def day_bars(bars: List[Bar], day) -> List[Bar]:
    out = [b for b in bars if to_et(b.ts).date() == day and session_open(day) <= to_et(b.ts) < session_open(day).replace(hour=16, minute=0)]
    return sorted(out, key=lambda b: b.ts)


def find_signal(symbol: str, bars: List[Bar], day, p: StrategyParams, known_until: Optional[datetime] = None) -> Optional[Signal]:
    """The newest of find_signals (the live runner only acts on a signal while its bar is the newest)."""
    sigs = find_signals(symbol, bars, day, p, known_until)
    return sigs[-1] if sigs else None


def find_signals(symbol: str, bars: List[Bar], day, p: StrategyParams, known_until: Optional[datetime] = None) -> List[Signal]:
    """Completed 5-min bars closing above the opening-range high: the first breakout and, when
    p.max_entries > 1, later re-breakouts (a close back at/below the high re-arms it). Uses only bars
    that completed by `known_until` (no look-ahead). Empty if the chart is unusable."""
    bs = [b for b in day_bars(bars, day) if known_until is None or b.end <= known_until]
    n_or = p.or_minutes // BAR_MIN
    if len(bs) <= n_or:
        return []
    start = session_open(day)
    # skill rule: every bar from the open to the signal bar must be present. Scan only the
    # contiguous prefix: a halt / thin-stock gap makes anything after it unusable.
    i = 0
    while i < len(bs) and to_et(bs[i].ts) == start + timedelta(minutes=BAR_MIN * i):
        i += 1
    return _scan(symbol, bs[:i], day, p, n_or)


def _scan(symbol, bs, day, p, n_or):
    if len(bs) <= n_or:
        return []
    rng = bs[:n_or]
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    cutoff = hhmm(p.entry_before)
    out, armed = [], True
    for b in bs[n_or:]:
        if b.c <= hi:
            armed = True
            continue
        if not armed:
            continue
        armed = False
        if to_et(b.end).time() > cutoff:
            break
        if (b.c / hi - 1) * 100 > p.max_extension_pct:
            if p.max_entries <= 1:
                break  # first breakout was a chase; later ones are not taken either
            continue
        out.append(Signal(symbol, day, b.end, b.c, hi, lo, round((hi + lo) / 2, 2)))
        if len(out) >= p.max_entries:
            break
    return out


def build_levels(ref: float, stop: float, p: StrategyParams) -> Optional[Levels]:
    """Entry limit sits limit_slippage_bps above the reference (ask or close): the order can
    never cost more than limit x qty. Returns None when the stop distance is out of bounds."""
    if ref < p.min_price:
        return None
    limit = round(ref * (1 + p.limit_slippage_bps / 1e4), 2)
    if stop >= limit:
        return None
    risk_pct = (limit - stop) / limit * 100
    if not (p.min_risk_pct <= risk_pct <= p.max_risk_pct):
        return None
    # target_r <= 0 means "no target": a far-away take-profit that only the flat-by exit realistically ends
    target = round(limit * 5, 2) if p.target_r <= 0 else round(limit + p.target_r * (limit - stop), 2)
    return Levels(limit=limit, stop=round(stop, 2), target=target)


def size_qty(levels: Levels, max_loss: float, max_notional: float, cash_available: float, exposure_room: float) -> int:
    """Whole shares: the smallest of the loss, order-size, settled-cash and exposure caps."""
    caps = [max_loss / levels.risk, max_notional / levels.limit, cash_available / levels.limit, exposure_room / levels.limit]
    return max(0, int(math.floor(min(caps) + 1e-9)))
