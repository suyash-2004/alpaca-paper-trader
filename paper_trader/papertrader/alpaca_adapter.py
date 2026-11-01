"""The only module that talks to Alpaca (official `alpaca-py` SDK, pinned in requirements.txt).

Exposed surface is intentionally tiny: read calls, a bracket or plain BUY limit order, a market SELL of
shares already held, and cancel-by-id. There is no short, margin, option, crypto,
close_all_positions or cancel_all_orders path. The client is built with paper=True and no
url_override; the resulting base URL is asserted equal to the hard-coded paper URL."""
from datetime import date

from .guards import SafetyError, assert_paper_endpoint
from .strategy import Bar


HTTP_TIMEOUT = (10, 30)  # (connect, read) seconds. alpaca-py sets none: a socket left half-open by sleep hung the loop ~2h


def _with_timeout(client):
    """Give every HTTP call of an alpaca-py client a default timeout (an explicit one still wins)."""
    s = client._session
    req = s.request
    s.request = lambda method, url, **kw: req(method, url, **{"timeout": HTTP_TIMEOUT, **kw})
    return client


def _v(x):
    return str(getattr(x, "value", x)).lower() if x is not None else None


def _s(x):
    return None if x is None else str(x)


def _order(o):
    return {
        "id": str(o.id), "client_order_id": o.client_order_id, "symbol": o.symbol,
        "side": _v(o.side), "status": _v(o.status), "type": _v(getattr(o, "order_type", None) or o.type),
        "qty": _s(o.qty), "filled_qty": _s(o.filled_qty), "filled_avg_price": _s(o.filled_avg_price),
        "limit_price": _s(o.limit_price), "stop_price": _s(o.stop_price),
        "created_at": o.created_at, "filled_at": o.filled_at,
        "legs": [_order(leg) for leg in (o.legs or [])],
    }


class AlpacaPaperBroker:
    def __init__(self, key: str, secret: str):
        from alpaca.trading.client import TradingClient
        c = TradingClient(key, secret, paper=True)  # never url_override
        assert_paper_endpoint(getattr(c, "_base_url", None))
        self._c = _with_timeout(c)

    # ---- read-only -------------------------------------------------------------------------
    def get_account(self):
        a = self._c.get_account()
        return {
            "account_number": a.account_number, "status": _v(a.status), "currency": a.currency,
            "cash": _s(a.cash), "equity": _s(a.equity), "last_equity": _s(a.last_equity),
            "multiplier": _s(a.multiplier), "shorting_enabled": a.shorting_enabled,
            "options_trading_level": a.options_trading_level, "trading_blocked": a.trading_blocked,
            "account_blocked": a.account_blocked, "accrued_fees": _s(a.accrued_fees),
            "pending_transfer_out": _s(a.pending_transfer_out),
        }

    def get_configurations(self):
        c = self._c.get_account_configurations()
        return {"no_shorting": c.no_shorting, "max_margin_multiplier": _s(c.max_margin_multiplier),
                "max_options_trading_level": c.max_options_trading_level, "suspend_trade": c.suspend_trade}

    def get_clock(self):
        c = self._c.get_clock()
        return {"is_open": c.is_open, "timestamp": c.timestamp, "next_open": c.next_open, "next_close": c.next_close}

    def get_trading_days(self, start: date, end: date):
        from alpaca.trading.requests import GetCalendarRequest
        return [d.date for d in self._c.get_calendar(GetCalendarRequest(start=start, end=end))]

    def get_positions(self):
        return [{"symbol": p.symbol, "qty": _s(p.qty), "avg_entry_price": _s(p.avg_entry_price),
                 "cost_basis": _s(p.cost_basis), "market_value": _s(p.market_value)}
                for p in self._c.get_all_positions()]

    def get_orders(self, status: str, after=None, limit: int = 500):
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        st = {"open": QueryOrderStatus.OPEN, "closed": QueryOrderStatus.CLOSED, "all": QueryOrderStatus.ALL}[status]
        req = GetOrdersRequest(status=st, limit=limit, after=after, nested=True)
        return [_order(o) for o in self._c.get_orders(req)]

    def get_asset(self, symbol: str):
        a = self._c.get_asset(symbol)
        return {"symbol": a.symbol, "asset_class": _v(a.asset_class), "status": _v(a.status),
                "tradable": a.tradable, "exchange": _v(a.exchange)}

    # ---- the only writes -------------------------------------------------------------------
    def set_configurations(self, **changes):
        allowed = {"no_shorting", "max_margin_multiplier", "max_options_trading_level"}
        if set(changes) - allowed:
            raise SafetyError("refusing to change account setting(s): %s" % sorted(set(changes) - allowed))
        cur = self._c.get_account_configurations()
        self._c.set_account_configurations(cur.model_copy(update=changes))
        return self.get_configurations()

    def submit_bracket_buy(self, symbol, qty, limit_price, take_profit, stop_loss, client_order_id):
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, StopLossRequest, TakeProfitRequest
        req = LimitOrderRequest(
            symbol=symbol, qty=int(qty), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            limit_price=limit_price, order_class=OrderClass.BRACKET, client_order_id=client_order_id,
            take_profit=TakeProfitRequest(limit_price=take_profit), stop_loss=StopLossRequest(stop_price=stop_loss))
        return _order(self._c.submit_order(req))

    def submit_limit_buy(self, symbol, qty, limit_price, client_order_id):
        """Plain DAY limit BUY of whole shares, paid from settled cash (hold mode). No bracket, no margin."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        req = LimitOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                limit_price=limit_price, client_order_id=client_order_id)
        return _order(self._c.submit_order(req))

    def submit_market_sell(self, symbol, qty, client_order_id):
        """Sells shares the runner already holds (qty verified by the caller); never opens a short."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        req = MarketOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY, client_order_id=client_order_id)
        return _order(self._c.submit_order(req))

    def cancel_order(self, order_id):
        self._c.cancel_order_by_id(order_id)


class AlpacaIexData:
    """Market data from Alpaca's free IEX feed (the only feed a paper-only account is entitled to)."""

    def __init__(self, key: str, secret: str):
        from alpaca.data.historical import StockHistoricalDataClient
        self._c = _with_timeout(StockHistoricalDataClient(key, secret))

    def get_bars(self, symbols, start, end):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start, end=end, feed=DataFeed.IEX)
        data = self._c.get_stock_bars(req).data
        return {s: [Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                    for b in data.get(s, [])] for s in symbols}

    def get_daily_bars(self, symbols, start, end):
        """Raw (unadjusted) daily bars, IEX feed. Returns {symbol: [(date, o, h, l, c, v)]}."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX)
        data = self._c.get_stock_bars(req).data
        return {s: [(b.timestamp.date(), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                    for b in data.get(s, [])] for s in symbols}

    def get_latest_quotes(self, symbols):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self._c.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
        return {s: {"bid": float(v.bid_price), "ask": float(v.ask_price), "ts": v.timestamp} for s, v in q.items()}


class AlpacaSipResearchData(AlpacaIexData):
    """RESEARCH ONLY: consolidated (SIP) history for backtests. Not used by the live runner."""

    def get_bars(self, symbols, start, end):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                               start=start, end=end, feed=DataFeed.SIP)
        data = self._c.get_stock_bars(req).data
        return {s: [Bar(b.timestamp, float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                    for b in data.get(s, [])] for s in symbols}

    def get_daily_bars(self, symbols, start, end):
        """Split-adjusted (no dividends, as in paper trading) SIP daily bars: {symbol: [(date, o, h, l, c, v)]}.
        `end` must be more than 15 minutes old."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=TimeFrame.Day, start=start, end=end,
                               feed=DataFeed.SIP, adjustment=Adjustment.SPLIT)
        data = self._c.get_stock_bars(req).data
        return {s: [(b.timestamp.date(), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))
                    for b in data.get(s, [])] for s in symbols}
