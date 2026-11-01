import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D

from papertrader.ledger import compute_cash_state, settlement_date
from papertrader.timeutil import ET
from tests.fakes import FakeBroker

TUE, WED, THU, FRI, MON = date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)
DAYS = FakeBroker().get_trading_days(date(2026, 10, 20), date(2026, 11, 12))


def sell(day, qty="10", px="50", oid="s"):
    return {"id": oid, "symbol": "X", "side": "sell", "status": "filled", "qty": qty, "filled_qty": qty,
            "filled_avg_price": px, "filled_at": datetime(day.year, day.month, day.day, 15, 0, tzinfo=ET), "legs": []}


def buy_open(qty="10", px="20", filled="0", oid="b"):
    return {"id": oid, "symbol": "Y", "side": "buy", "status": "new", "qty": qty, "filled_qty": filled,
            "limit_price": px, "legs": []}


def state(orders=(), positions=(), today=TUE, capital=10000, cash="1000", **acct):
    a = FakeBroker(cash=cash).get_account()
    a.update(acct)
    return compute_cash_state(a, list(positions), list(orders), DAYS, today, capital)


class CashOnly(unittest.TestCase):
    def test_buying_power_fields_are_ignored(self):
        s = state(cash="1000")  # the fake account advertises 999999 of buying power
        self.assertEqual(s.available, D(1000))

    def test_capital_principal_caps_cash(self):
        self.assertEqual(state(cash="50000", capital=10000).available, D(10000))

    def test_open_positions_reduce_capital_room(self):
        s = state(cash="50000", capital=10000, positions=[{"symbol": "Z", "qty": "10", "cost_basis": "3000"}])
        self.assertEqual(s.available, D(7000))

    def test_fees_and_pending_transfers_subtracted(self):
        s = state(cash="1000", accrued_fees="10", pending_transfer_out="40")
        self.assertEqual(s.available, D(950))


class ReservedOrders(unittest.TestCase):
    def test_open_buy_orders_reserve_cash(self):
        s = state([buy_open("10", "20")], cash="1000")
        self.assertEqual(s.reserved, D(200))
        self.assertEqual(s.available, D(800))

    def test_partial_fill_reserves_only_remainder(self):
        s = state([buy_open("10", "20", filled="4")], cash="1000")
        self.assertEqual(s.reserved, D(120))

    def test_bracket_child_buy_legs_counted_once_and_sell_legs_ignored(self):
        parent = buy_open("10", "20")
        parent["legs"] = [{"id": "t", "symbol": "Y", "side": "sell", "status": "held", "qty": "10", "filled_qty": "0", "limit_price": "22", "legs": []}]
        self.assertEqual(state([parent], cash="1000").reserved, D(200))

    def test_buy_without_limit_price_makes_state_unsafe(self):
        o = buy_open()
        o["limit_price"] = None
        s = state([o])
        self.assertFalse(s.ok)
        self.assertEqual(s.available, D(0))

    def test_local_reserve_after_submit(self):
        s = state(cash="1000")
        s.reserve(300)
        self.assertEqual(s.available, D(700))
        s.reserve(900)
        self.assertEqual(s.available, D(0))


class Settlement(unittest.TestCase):
    def test_settlement_date_is_next_trading_day(self):
        self.assertEqual(settlement_date(TUE, DAYS, 1), WED)
        self.assertEqual(settlement_date(FRI, DAYS, 1), MON)  # over the weekend

    def test_sale_today_not_reusable_today(self):
        s = state([sell(TUE, "10", "50")], cash="1500", today=TUE)
        self.assertEqual(s.unsettled, D(500))
        self.assertEqual(s.available, D(1000))

    def test_sale_reusable_on_next_trading_day(self):
        s = state([sell(TUE, "10", "50")], cash="1500", today=WED)
        self.assertEqual(s.unsettled, D(0))
        self.assertEqual(s.available, D(1500))

    def test_friday_sale_unsettled_through_weekend_until_monday(self):
        self.assertEqual(state([sell(FRI)], cash="1500", today=FRI).unsettled, D(500))
        self.assertEqual(state([sell(FRI)], cash="1500", today=MON).unsettled, D(0))

    def test_sell_leg_of_bracket_counts(self):
        parent = buy_open()
        parent["status"] = "filled"
        parent["legs"] = [sell(TUE, "10", "50", "leg")]
        self.assertEqual(state([parent], cash="1500", today=TUE).unsettled, D(500))

    def test_undeterminable_settlement_stops_buying(self):
        s = compute_cash_state(FakeBroker().get_account(), [], [sell(TUE)], [], TUE, 10000)
        self.assertFalse(s.ok)
        self.assertEqual(s.available, D(0))
        o = sell(TUE)
        o["filled_at"] = None
        self.assertFalse(state([o]).ok)

    def test_truncated_order_history_stops_buying(self):
        s = compute_cash_state(FakeBroker().get_account(), [], [], DAYS, TUE, 10000, orders_truncated=True)
        self.assertFalse(s.ok)
        self.assertEqual(s.available, D(0))

    def test_unknown_cash_or_short_position_stops_buying(self):
        self.assertFalse(state(cash=None).ok)
        self.assertFalse(state(positions=[{"symbol": "Z", "qty": "-5", "cost_basis": "100"}]).ok)


if __name__ == "__main__":
    unittest.main()
