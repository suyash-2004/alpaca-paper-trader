import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from papertrader.config import ConfigError, HoldCfg, load_config
from papertrader.hold import HoldRunner
from papertrader.timeutil import UTC
from tests.fakes import DAY, NOW, FakeBroker, FakeData, cfg, et, make_runner

SPY_ID = "ptr-e-20261027-SPY-H1"


def hold(broker=None, submit=True, **kw):
    r, b, d, tmp = make_runner(broker=broker, data=FakeData({}), config=cfg(hold=HoldCfg(enabled=True), **kw),
                               submit=submit, cls=HoldRunner)
    r.startup()
    return r, b, d, tmp


def own(r, b, cost="8000", value="8000"):
    """Runner-owned SPY position."""
    r.state.data["plans"]["SPY"] = {"client_order_id": SPY_ID, "day": "2026-10-26"}
    b.positions.append({"symbol": "SPY", "qty": "79", "avg_entry_price": "101", "cost_basis": cost, "market_value": value})


def alerts(tmp, text):
    p = Path(tmp) / "alerts.log"
    return p.read_text().count(text) if p.exists() else 0


class Buy(unittest.TestCase):
    def test_first_buy_is_plain_limit_capped_by_order_notional_with_no_stop(self):
        r, b, _, _ = hold()
        out = r.tick(NOW)
        s = b.submitted[0]
        self.assertEqual((s["symbol"], s["limit"], s["qty"], s["cid"]), ("SPY", 100.95, 49, SPY_ID))  # ask 100.85 + 10bps
        self.assertTrue(s["plain"] and "sl" not in s and "tp" not in s)
        self.assertLessEqual(s["qty"] * s["limit"], 5000)
        self.assertEqual(out["submitted"][0]["client_order_id"], SPY_ID)
        self.assertEqual(b.sells, [])
        self.assertIn("SPY", r.state.data["plans"])

    def test_buy_is_capped_by_settled_cash(self):
        r, b, *_ = hold(broker=FakeBroker(cash="1000"))
        r.tick(NOW)
        self.assertEqual(b.submitted[0]["qty"], 9)  # int(1000 / 100.95)

    def test_stops_buying_at_the_exposure_target(self):
        r, b, *_ = hold(broker=FakeBroker(cash="2010"))
        own(r, b, cost="7990", value="7990")  # 8000 target - 7990 = $10 of room: less than one share
        out = r.tick(NOW)
        self.assertEqual((b.submitted, out["idle"]), ([], "target reached"))

    def test_second_tick_does_not_double_buy_while_the_order_works(self):
        r, b, *_ = hold()
        r.tick(NOW)
        out = r.tick(NOW + timedelta(seconds=10))
        self.assertEqual(len(b.submitted), 1)
        self.assertEqual(out["idle"], "entry order working")

    def test_duplicate_id_is_refused(self):
        r, b, *_ = hold()
        r.state.data["sent_ids"].append(SPY_ID)
        out = r.tick(NOW)
        self.assertEqual(b.submitted, [])
        self.assertIn("duplicate client_order_id", out["rejected"][0][1])

    def test_attempt_cap_per_day(self):
        r, b, _, tmp = hold()
        r.state.data["hold"] = {"tries": {str(DAY): 3}}
        out = r.tick(NOW)
        self.assertEqual((b.submitted, out["idle"]), ([], "max attempts today"))
        self.assertEqual(alerts(tmp, "without filling"), 1)

    def test_rejected_order_counts_as_an_attempt_and_alerts(self):
        r, b, _, tmp = hold()
        b.fail_submit = True
        out = r.tick(NOW)
        self.assertEqual(len(out["rejected"]), 1)
        self.assertEqual(r.state.data["hold"]["tries"][str(DAY)], 1)
        self.assertEqual(alerts(tmp, "order rejected"), 1)

    def test_unfilled_entry_is_canceled_after_the_timeout(self):
        r, b, *_ = hold()
        r.tick(NOW)
        r.tick(NOW + timedelta(seconds=200))
        self.assertEqual(b.canceled, ["h1"])

    def test_foreign_spy_position_is_never_touched(self):
        r, b, *_ = make_runner(config=cfg(hold=HoldCfg(enabled=True)), data=FakeData({}), cls=HoldRunner)[:3]
        b.positions.append({"symbol": "SPY", "qty": "5", "avg_entry_price": "100", "cost_basis": "500", "market_value": "500"})
        r.startup()
        out = r.tick(NOW)
        self.assertEqual((b.submitted, b.sells), ([], []))
        self.assertEqual(out["skipped"][0][0], "SPY")

    def test_dry_run_proposes_once_and_sends_nothing(self):
        r, b, *_ = hold(submit=False)
        self.assertEqual(len(r.tick(NOW)["proposed"]), 1)
        self.assertEqual(len(r.tick(NOW + timedelta(seconds=10))["proposed"]), 0)
        self.assertEqual(b.submitted, [])

    def test_outside_entry_window_and_closed_market_are_idle(self):
        r, b, *_ = hold()
        self.assertEqual(r.tick(et(DAY, 15, 45).astimezone(UTC))["idle"], "outside entry window")
        self.assertEqual(r.tick(et(DAY, 9, 40).astimezone(UTC))["idle"], "outside entry window")
        b.is_open = False
        self.assertEqual(r.tick(NOW)["idle"], "market closed")
        self.assertEqual(b.submitted, [])

    def test_kill_switch_stops_and_sells_nothing(self):
        r, b, *_ = hold()
        own(r, b)
        r.kill_path.write_text("x")
        self.assertTrue(r.tick(NOW)["stopped"])
        self.assertEqual((b.submitted, b.sells), ([], []))


class Drawdown(unittest.TestCase):
    def test_alert_fires_once_and_never_sells(self):
        r, b, _, tmp = hold(broker=FakeBroker(cash="2000"))
        own(r, b, cost="8000", value="5900")  # -$2,100 >= 20% of $10,000
        r.tick(NOW)
        r.tick(NOW + timedelta(seconds=10))
        self.assertEqual(alerts(tmp, "drawdown alert"), 1)
        self.assertEqual((b.sells, b.submitted), ([], []))

    def test_high_water_mark_and_rearm(self):
        r, b, _, tmp = hold(broker=FakeBroker(cash="2000"))
        own(r, b)
        for value, expected_alerts in (("9500", 0), ("8000", 0), ("7400", 1), ("9600", 1), ("7000", 2)):
            b.positions[0]["market_value"] = value
            r.tick(NOW)
            self.assertEqual(alerts(tmp, "drawdown alert"), expected_alerts, value)


class Config(unittest.TestCase):
    def test_hold_is_off_by_default_and_symbol_is_validated(self):
        self.assertFalse(cfg().hold.enabled)
        self.assertFalse(cfg(hold=HoldCfg(enabled=True)).problems())
        self.assertTrue(any("hold.symbol" in p for p in cfg(hold=HoldCfg(enabled=True, symbol="spy")).problems()))

    def test_unknown_hold_key_is_rejected(self):
        f = Path(tempfile.mkdtemp()) / "c.json"
        f.write_text(json.dumps({"hold": {"enabled": True, "leverage": 2}}))
        with self.assertRaises(ConfigError):
            load_config(f)


if __name__ == "__main__":
    unittest.main()
