"""Conservative settled-cash ledger. Sizing uses ONLY this number.

available = min( cash - accrued fees - pending transfers out
                 - unsettled sell proceeds - cash reserved by open buy orders,
                 capital_usd - cost basis of open positions - reserved )

Alpaca's buying_power / regt_buying_power / daytrading_buying_power are never read.
Anything that cannot be determined safely sets ok=False -> available=0 -> no new buys."""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List

from .timeutil import ET

OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
                 "held", "pending_replace", "pending_cancel", "calculated", "stopped", "replaced"}
ZERO = Decimal(0)


def dec(x):
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError):
        return None


@dataclass
class CashState:
    ok: bool = True
    reasons: List[str] = field(default_factory=list)
    cash: Decimal = ZERO
    unsettled: Decimal = ZERO
    reserved: Decimal = ZERO
    open_cost: Decimal = ZERO
    capital: Decimal = ZERO
    available: Decimal = ZERO

    def reserve(self, notional):
        """Reserve cash for an order just submitted that the broker list may not show yet."""
        self.reserved += Decimal(str(notional))
        self.available = max(ZERO, self.available - Decimal(str(notional)))

    def exposure_room(self):
        return max(ZERO, self.capital - self.open_cost - self.reserved)


def flatten_orders(orders):
    for o in orders:
        yield o
        yield from flatten_orders(o.get("legs") or [])


def settlement_date(fill_day: date, trading_days: List[date], settlement_days: int):
    """The trading day on which cash from a sale on fill_day becomes usable (T+N)."""
    later = sorted(d for d in trading_days if d > fill_day)
    return later[settlement_days - 1] if len(later) >= settlement_days else None


def compute_cash_state(account, positions, orders, trading_days, today, capital_usd,
                       settlement_days=1, orders_truncated=False) -> CashState:
    st = CashState(capital=dec(capital_usd) or ZERO)

    def bad(msg):
        st.ok = False
        st.reasons.append(msg)

    cash = dec(account.get("cash"))
    if cash is None:
        bad("account cash unknown")
        cash = ZERO
    st.cash = cash
    fees = dec(account.get("accrued_fees")) or ZERO
    out = max(ZERO, dec(account.get("pending_transfer_out")) or ZERO)
    if orders_truncated:
        bad("order history truncated: cannot account for every unsettled sale / open buy")
    if not trading_days:
        bad("trading calendar unavailable: cannot apply the settlement rule")

    for o in flatten_orders(orders):
        status = str(o.get("status", "")).lower().split(".")[-1]
        side = str(o.get("side", "")).lower().split(".")[-1]
        filled = dec(o.get("filled_qty")) or ZERO
        if side == "buy" and status in OPEN_STATUSES:
            qty, px = dec(o.get("qty")), dec(o.get("limit_price"))
            if qty is None or px is None:
                bad("open buy order %s has no qty/limit price to reserve against" % o.get("id"))
                continue
            st.reserved += max(ZERO, qty - filled) * px
        elif side == "sell" and filled > 0:
            when, avg = o.get("filled_at"), dec(o.get("filled_avg_price"))
            if when is None or avg is None:
                bad("sell order %s has fills but no fill time/price" % o.get("id"))
                continue
            fill_day = when.astimezone(ET).date()
            settle = settlement_date(fill_day, trading_days, settlement_days)
            if settle is None:
                bad("cannot determine settlement date for sale on %s" % fill_day)
            elif settle > today:
                st.unsettled += filled * avg

    st.open_cost = sum((dec(p.get("cost_basis")) or ZERO for p in positions), ZERO)
    if any(dec(p.get("cost_basis")) is None for p in positions):
        bad("a position has no cost basis")
    if any((dec(p.get("qty")) or ZERO) < 0 for p in positions):
        bad("short position present")

    by_cash = cash - fees - out - st.unsettled - st.reserved
    by_cap = st.capital - st.open_cost - st.reserved
    st.available = max(ZERO, min(by_cash, by_cap)) if st.ok else ZERO
    return st
