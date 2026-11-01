"""Turn closed paper orders into trade-journal rows for the paper-forward report."""
from decimal import Decimal

from .config import Costs
from .ledger import dec, flatten_orders
from .timeutil import ET

ENTRY_PREFIX = "ptr-e-"
EXIT_PREFIX = "ptr-x-"


def sell_fees(qty, price, c: Costs):
    """Regulatory fees on a sale. Alpaca paper does not charge them, so they are estimated here."""
    return qty * price * c.sec_fee_per_million / 1e6 + min(qty * c.taf_per_share, c.taf_max)


def _avg(orders):
    q = sum((dec(o.get("filled_qty")) or Decimal(0) for o in orders), Decimal(0))
    if q <= 0:
        return Decimal(0), None
    return q, sum((dec(o["filled_qty"]) * dec(o["filled_avg_price"]) for o in orders), Decimal(0)) / q


def build_rows(closed_orders, positions, already, costs: Costs):
    """One row per runner entry that is now flat and has at least one filled exit."""
    held = {p["symbol"] for p in positions}
    exits_by_symbol = {}
    for o in closed_orders:
        if str(o.get("client_order_id", "")).startswith(EXIT_PREFIX) and (dec(o.get("filled_qty")) or 0) > 0:
            exits_by_symbol.setdefault(o["symbol"], []).append(o)
    rows = []
    for parent in closed_orders:
        cid = str(parent.get("client_order_id", ""))
        if not cid.startswith(ENTRY_PREFIX) or parent["id"] in already or parent["symbol"] in held:
            continue
        qty_in, px_in = _avg([parent])
        if qty_in <= 0:
            continue
        leg_exits = [l for l in flatten_orders(parent.get("legs") or []) if str(l.get("side")).endswith("sell")
                     and (dec(l.get("filled_qty")) or 0) > 0]
        time_exits = [o for o in exits_by_symbol.get(parent["symbol"], [])
                      if o["filled_at"] and parent["filled_at"] and o["filled_at"] >= parent["filled_at"]]
        exits = leg_exits + time_exits
        qty_out, px_out = _avg(exits)
        if qty_out <= 0:
            continue
        kinds = {("time" if str(e.get("client_order_id", "")).startswith(EXIT_PREFIX)
                  else "stop" if "stop" in str(e.get("type")) else "target") for e in exits}
        gross = float((px_out - px_in) * qty_out)
        fees = sell_fees(float(qty_out), float(px_out), costs)
        rows.append({
            "order_id": parent["id"], "symbol": parent["symbol"],
            "day": parent["filled_at"].astimezone(ET).date().isoformat(),
            "entry_ts": parent["filled_at"].isoformat(), "entry_qty": float(qty_in), "entry_px": float(px_in),
            "exit_qty": float(qty_out), "exit_px": float(px_out),
            "exit_reason": "+".join(sorted(kinds)), "gross_pnl": gross, "est_fees": fees, "net_pnl": gross - fees,
            "entry_cost": float(px_in * qty_in),
        })
    return rows
