"""Read-only status for the human: day P&L, open positions, today's orders, runner liveness. Never trades."""
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal

from .journal import ENTRY_PREFIX
from .ledger import flatten_orders
from .timeutil import ET, UTC

D = Decimal
NOTABLE = {"order_submitted", "flat_by_exit_submitted", "ALERT", "entry_canceled_timeout"}
STALE = timedelta(minutes=20)  # the run loop writes a heartbeat every 15 minutes


def _events(path, offset):
    """Audit events appended after byte `offset` -> (events, new_offset). A half-written last line waits."""
    if not path.exists():
        return [], 0
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    events, used = [], 0
    for line in data.split(b"\n")[:-1]:
        used += len(line) + 1
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    return events, offset + used


def last_heartbeat(path):
    beat = None
    for e in _events(path, max(0, path.stat().st_size - 200_000) if path.exists() else 0)[0]:
        if e.get("event") == "heartbeat":
            beat = datetime.fromisoformat(e["ts"])
    return beat


def _usd(x):
    return ("+" if x >= 0 else "-") + "$%.2f" % abs(x)


def status_text(broker, cfg, audit_path, now=None):
    now = now or datetime.now(UTC)
    acct, clock = broker.get_account(), broker.get_clock()
    day_pnl = D(str(acct["equity"])) - D(str(acct["last_equity"]))
    positions = broker.get_positions()
    open_pnl = sum((D(str(p["market_value"])) - D(str(p["cost_basis"])) for p in positions), D(0))
    since = datetime.combine(now.astimezone(ET).date(), datetime.min.time(), tzinfo=ET)
    orders = broker.get_orders("all", after=since)
    entries = [o for o in orders if str(o.get("client_order_id", "")).startswith(ENTRY_PREFIX)]
    filled = [o for o in entries if D(str(o.get("filled_qty") or 0)) > 0]
    sells = [o for o in flatten_orders(orders) if str(o.get("side")).endswith("sell") and D(str(o.get("filled_qty") or 0)) > 0]
    beat = last_heartbeat(audit_path)
    age = "never" if beat is None else "%d min ago%s" % ((now - beat).total_seconds() // 60, " - NOT RESPONDING" if now - beat > STALE else "")
    goal = cfg.daily_profit_target_usd
    lines = ["[%s ET] market %s | day P&L %s (closed trades %s, open %s) | goal %s | daily loss limit -$%s" % (
        now.astimezone(ET).strftime("%H:%M"), "OPEN" if clock["is_open"] else "closed", _usd(day_pnl),
        _usd(day_pnl - open_pnl), _usd(open_pnl), _usd(D(str(goal))) if goal else "off", cfg.max_daily_loss_usd)]
    for p in positions:
        pnl = D(str(p["market_value"])) - D(str(p["cost_basis"]))
        lines.append("  open: %s %s sh @ %s, now worth %s (%s)" % (p["symbol"], p["qty"], p["avg_entry_price"], p["market_value"], _usd(pnl)))
    if not positions:
        lines.append("  open: none")
    lines.append("  today: %d entries sent, %d filled, %d sells filled | runner heartbeat %s" % (len(entries), len(filled), len(sells), age))
    return "\n".join(lines)


def watch(broker, audit_path, max_minutes, sleep=time.sleep, now_fn=lambda: datetime.now(UTC)):
    """Block until something notable happens (order, exit, alert, position change, runner silent) or
    max_minutes pass. Returns the reason. Read-only."""
    offset = audit_path.stat().st_size if audit_path.exists() else 0
    held = sorted((p["symbol"], str(p["qty"])) for p in broker.get_positions())
    for _ in range(int(max_minutes)):
        sleep(60)
        events, offset = _events(audit_path, offset)
        hits = [e.get("msg") or e.get("event") for e in events if e.get("event") in NOTABLE]
        if hits:
            return "; ".join(dict.fromkeys(hits))[:300]
        now_held = sorted((p["symbol"], str(p["qty"])) for p in broker.get_positions())
        if now_held != held:
            return "positions changed"
        beat = last_heartbeat(audit_path)
        if beat is None or now_fn() - beat > STALE:
            return "runner heartbeat is stale: it may be stopped or hung"
    return "timer (%d min)" % max_minutes
