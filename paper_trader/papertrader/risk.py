"""Pre-trade gate. Every buy must pass check_entry() BEFORE any broker call.
A non-empty result means: reject locally, do not call Alpaca."""
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from .config import Config
from .ledger import OPEN_STATUSES, CashState, flatten_orders
from .strategy import Levels

_TICKER = re.compile(r"^[A-Z]{1,5}$")
D = Decimal


@dataclass
class Intent:
    symbol: str
    qty: int
    levels: Levels
    client_order_id: str

    @property
    def notional(self):
        return D(str(self.levels.limit)) * self.qty

    @property
    def max_loss(self):
        return D(str(self.levels.limit - self.levels.stop)) * self.qty


@dataclass
class RiskContext:
    cfg: Config
    cash: CashState
    positions: List[dict] = field(default_factory=list)
    open_orders: List[dict] = field(default_factory=list)
    day_pnl: Decimal = D(0)          # equity - last_equity; negative = loss
    open_risk: Decimal = D(0)        # worst-case loss to stops of positions already open
    kill: bool = False
    halted: Optional[str] = None     # latched reason: no new entries today
    data_ok: bool = True
    seen_ids: frozenset = frozenset()
    foreign_symbols: frozenset = frozenset()  # positions we did not open: never touched/added to


def _open_buy_symbols(orders):
    out = set()
    for o in flatten_orders(orders):
        if str(o.get("side", "")).lower().endswith("buy") and str(o.get("status", "")).lower().split(".")[-1] in OPEN_STATUSES:
            out.add(o.get("symbol"))
    return out


def daily_limit_hit(cfg: Config, day_pnl) -> bool:
    return -D(str(day_pnl)) >= D(str(cfg.max_daily_loss_usd))


def check_entry(it: Intent, ctx: RiskContext) -> List[str]:
    cfg, v = ctx.cfg, []
    if cfg.problems():
        return ["config incomplete: " + "; ".join(cfg.problems())]
    if ctx.kill:
        v.append("kill switch is on")
    if ctx.halted:
        v.append("new entries halted: " + ctx.halted)
    if not ctx.data_ok:
        v.append("market data stale/unavailable")
    if not _TICKER.match(it.symbol or ""):
        v.append("symbol %r is not a plain US ticker" % it.symbol)
    if it.symbol in ctx.foreign_symbols:
        v.append("symbol held outside this runner: not touched")
    if not isinstance(it.qty, int) or it.qty < 1:
        v.append("qty must be a whole number >= 1")
        return v
    lv = it.levels
    if not (lv.stop < lv.limit < lv.target):
        v.append("levels must satisfy stop < entry < target")
    if it.client_order_id in ctx.seen_ids:
        v.append("duplicate client_order_id")
    held = {p.get("symbol") for p in ctx.positions}
    if it.symbol in held or it.symbol in _open_buy_symbols(ctx.open_orders):
        v.append("already holding or ordering %s" % it.symbol)
    if len(held | _open_buy_symbols(ctx.open_orders)) >= cfg.max_open_positions:
        v.append("max_open_positions reached (%d)" % cfg.max_open_positions)

    n = it.notional
    if n > D(str(cfg.max_order_notional_usd)):
        v.append("order notional $%s exceeds max_order_notional_usd" % n)
    # HARD CAP: total notional (this order + everything already reserved) must fit settled cash.
    # cash.available is already net of reserved open buy orders and unsettled sale proceeds.
    if not ctx.cash.ok:
        v.append("settled cash unknown: " + "; ".join(ctx.cash.reasons))
    elif n > ctx.cash.available:
        v.append("order notional $%s exceeds available settled cash $%s" % (n, ctx.cash.available))
    if ctx.cash.open_cost + ctx.cash.reserved + n > D(str(cfg.max_account_exposure_usd)):
        v.append("would exceed max_account_exposure_usd")
    if it.max_loss > D(str(cfg.max_loss_per_trade_usd)):
        v.append("max loss at stop $%s exceeds max_loss_per_trade_usd" % it.max_loss)
    if daily_limit_hit(cfg, ctx.day_pnl):
        v.append("daily loss limit already hit")
    elif -ctx.day_pnl + ctx.open_risk + it.max_loss > D(str(cfg.max_daily_loss_usd)):
        v.append("worst case (open risk + this stop) could breach max_daily_loss_usd")
    return v
