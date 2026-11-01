"""In-memory stand-ins for the paper API (mocked responses; no network)."""
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from papertrader.audit import Audit
from papertrader.config import Config
from papertrader.runner import Runner
from papertrader.state import StateStore
from papertrader.strategy import Bar
from papertrader.timeutil import ET, UTC

DAY = date(2026, 10, 27)  # a Tuesday


def cfg(**kw):
    base = dict(order_submission_enabled=True, capital_usd=10000, max_loss_per_trade_usd=50, max_daily_loss_usd=150,
                max_open_positions=3, max_order_notional_usd=5000, max_account_exposure_usd=8000,
                universe=["AAA", "BBB"])
    base.update(kw)
    return Config(**base)


def et(day, hh, mm, ss=0):
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=ET)


def make_bars(day, rows, start=(9, 30)):
    t = et(day, *start)
    return [Bar(t + timedelta(minutes=5 * i), o, h, l, c, 1000) for i, (o, h, l, c) in enumerate(rows)]


# opening range 9:30-9:44 = hi 100.4 / lo 99.6 (mid 100.0); the 9:45 bar closes 100.8 -> signal at 9:50
BREAKOUT = [(100, 100.4, 99.6, 100.2), (100.2, 100.3, 99.7, 100.0), (100.0, 100.4, 99.8, 100.3), (100.3, 100.9, 100.2, 100.8)]
NOW = et(DAY, 9, 50, 20).astimezone(UTC)


class FakeBroker:
    def __init__(self, cash="10000", equity="10000", last_equity="10000"):
        self.acct = {"account_number": "PA1234567", "status": "ACTIVE", "currency": "USD", "cash": cash,
                     "equity": equity, "last_equity": last_equity, "multiplier": "1", "shorting_enabled": False,
                     "options_trading_level": 0, "trading_blocked": False, "account_blocked": False,
                     "accrued_fees": "0", "pending_transfer_out": "0",
                     "buying_power": "999999", "regt_buying_power": "999999", "daytrading_buying_power": "999999"}
        self.config = {"no_shorting": True, "max_margin_multiplier": "1", "max_options_trading_level": 0, "suspend_trade": False}
        self.positions, self.open, self.closed = [], [], []
        self.is_open = True
        self.submitted, self.sells, self.canceled, self.cfg_changes = [], [], [], []
        self.fail_submit = False
        self.ignore_config_changes = False

    def get_account(self): return dict(self.acct)
    def get_configurations(self): return dict(self.config)

    def set_configurations(self, **ch):
        self.cfg_changes.append(ch)
        if not self.ignore_config_changes:
            self.config.update(ch)
        return dict(self.config)

    def get_clock(self): return {"is_open": self.is_open, "timestamp": NOW, "next_open": None, "next_close": None}

    def get_trading_days(self, start, end):
        d, out = start, []
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def get_positions(self): return [dict(p) for p in self.positions]
    def get_orders(self, status, after=None, limit=500): return list(self.open if status == "open" else self.closed)
    def get_asset(self, symbol): return {"symbol": symbol, "asset_class": "us_equity", "status": "active", "tradable": True, "exchange": "NASDAQ"}

    def submit_bracket_buy(self, symbol, qty, limit_price, take_profit, stop_loss, client_order_id):
        if self.fail_submit:
            raise RuntimeError("422 rejected")
        self.submitted.append(dict(symbol=symbol, qty=qty, limit=limit_price, tp=take_profit, sl=stop_loss, cid=client_order_id))
        o = {"id": "o%d" % len(self.submitted), "client_order_id": client_order_id, "symbol": symbol, "side": "buy",
             "status": "new", "type": "limit", "qty": str(qty), "filled_qty": "0", "filled_avg_price": None,
             "limit_price": str(limit_price), "stop_price": None, "created_at": NOW, "filled_at": None,
             "legs": [{"id": "l1-%d" % len(self.submitted), "client_order_id": "x", "symbol": symbol, "side": "sell", "status": "held",
                       "type": "limit", "qty": str(qty), "filled_qty": "0", "limit_price": str(take_profit), "legs": []},
                      {"id": "l2-%d" % len(self.submitted), "client_order_id": "x", "symbol": symbol, "side": "sell", "status": "held",
                       "type": "stop", "qty": str(qty), "filled_qty": "0", "limit_price": None, "legs": []}]}
        self.open.append(o)
        return o

    def submit_limit_buy(self, symbol, qty, limit_price, client_order_id):
        if self.fail_submit:
            raise RuntimeError("422 rejected")
        self.submitted.append(dict(symbol=symbol, qty=qty, limit=limit_price, cid=client_order_id, plain=True))
        o = {"id": "h%d" % len(self.submitted), "client_order_id": client_order_id, "symbol": symbol, "side": "buy",
             "status": "new", "type": "limit", "qty": str(qty), "filled_qty": "0", "filled_avg_price": None,
             "limit_price": str(limit_price), "stop_price": None, "created_at": NOW, "filled_at": None, "legs": []}
        self.open.append(o)
        return o

    def submit_market_sell(self, symbol, qty, client_order_id):
        self.sells.append(dict(symbol=symbol, qty=qty, cid=client_order_id))
        return {"id": "s%d" % len(self.sells)}

    def cancel_order(self, order_id): self.canceled.append(order_id)


class FakeData:
    def __init__(self, bars):
        self.bars, self.fail, self.quote_ask = bars, False, 100.85

    def get_bars(self, symbols, start, end):
        if self.fail:
            raise ConnectionError("data feed down")
        return {s: [b for b in self.bars.get(s, []) if b.end <= end] for s in symbols}

    def get_latest_quotes(self, symbols):
        return {s: {"bid": self.quote_ask - 0.05, "ask": self.quote_ask, "ts": NOW} for s in symbols}


def make_runner(broker=None, data=None, config=None, submit=True, replay=False, cls=Runner):
    tmp = Path(tempfile.mkdtemp())
    broker = broker or FakeBroker()
    data = data or FakeData({"AAA": make_bars(DAY, BREAKOUT), "BBB": make_bars(DAY, [(50, 50.1, 49.9, 50)] * 4)})
    r = cls(config or cfg(), broker, data, Audit(tmp, echo=False), StateStore(tmp / "state.json"), tmp / "KILL",
            submit=submit, replay=replay, sleep_fn=lambda s: None)
    return r, broker, data, tmp
