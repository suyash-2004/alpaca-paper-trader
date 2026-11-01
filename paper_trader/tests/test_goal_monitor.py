import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from papertrader.monitor import status_text, watch
from papertrader.runner import PROFIT_GOAL
from tests.fakes import DAY, NOW, FakeBroker, cfg, make_runner


def owned(r, b):
    r.state.data["plans"]["AAA"] = {"client_order_id": "ptr-e-20261027-AAA-0950", "day": str(DAY), "stop": 100.0,
                                    "target": 101.5, "limit": 100.95, "qty": 10}
    b.positions.append({"symbol": "AAA", "qty": "10", "avg_entry_price": "100.95", "cost_basis": "1009.5", "market_value": "1110"})


class ProfitGoal(unittest.TestCase):
    def test_goal_reached_halts_entries_and_closes_only_runner_positions(self):
        r, b, *_ = make_runner(broker=FakeBroker(equity="10100.50", last_equity="10000"), config=cfg(daily_profit_target_usd=100))
        r.startup()
        owned(r, b)
        out = r.tick(NOW)
        self.assertEqual(out["halted"], PROFIT_GOAL)
        self.assertEqual([s["symbol"] for s in b.sells], ["AAA"])
        self.assertEqual(b.submitted, [])  # the breakout signal is ignored once the goal is reached

    def test_below_goal_keeps_trading(self):
        r, b, *_ = make_runner(broker=FakeBroker(equity="10099", last_equity="10000"), config=cfg(daily_profit_target_usd=100))
        r.startup()
        out = r.tick(NOW)
        self.assertNotIn("halted", out)
        self.assertEqual((len(b.submitted), b.sells), (1, []))

    def test_goal_is_off_by_default_and_validated(self):
        self.assertIsNone(cfg().daily_profit_target_usd)
        self.assertTrue(cfg(daily_profit_target_usd=0).problems())


class Monitor(unittest.TestCase):
    def audit(self, *events):
        p = Path(tempfile.mkdtemp()) / "audit.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in events))
        return p

    def test_status_shows_pnl_split_positions_and_heartbeat(self):
        b = FakeBroker(equity="10042.50", last_equity="10000")
        b.positions.append({"symbol": "AAA", "qty": "10", "avg_entry_price": "100", "cost_basis": "1000", "market_value": "1012.5"})
        p = self.audit({"event": "heartbeat", "ts": (NOW - timedelta(minutes=3)).isoformat()})
        text = status_text(b, cfg(daily_profit_target_usd=100), p, now=NOW)
        self.assertIn("day P&L +$42.50 (closed trades +$30.00, open +$12.50) | goal +$100.00", text)
        self.assertIn("AAA 10 sh @ 100", text)
        self.assertIn("heartbeat 3 min ago", text)

    def test_status_flags_a_silent_runner(self):
        p = self.audit({"event": "heartbeat", "ts": (NOW - timedelta(minutes=45)).isoformat()})
        self.assertIn("NOT RESPONDING", status_text(FakeBroker(), cfg(), p, now=NOW))

    def test_watch_wakes_on_an_order(self):
        p = self.audit({"event": "heartbeat", "ts": NOW.isoformat()})

        def sleep(_):
            with open(p, "a") as f:
                f.write(json.dumps({"event": "order_submitted", "ts": NOW.isoformat()}) + "\n")
        self.assertEqual(watch(FakeBroker(), p, 30, sleep=sleep, now_fn=lambda: NOW), "order_submitted")

    def test_watch_times_out_or_wakes_on_a_stale_heartbeat(self):
        p = self.audit({"event": "heartbeat", "ts": NOW.isoformat()})
        self.assertEqual(watch(FakeBroker(), p, 2, sleep=lambda s: None, now_fn=lambda: NOW), "timer (2 min)")
        self.assertIn("stale", watch(FakeBroker(), p, 2, sleep=lambda s: None, now_fn=lambda: NOW + timedelta(minutes=30)))


if __name__ == "__main__":
    unittest.main()
