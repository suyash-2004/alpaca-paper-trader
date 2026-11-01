import unittest
from dataclasses import replace
from datetime import timedelta

from papertrader.config import StrategyParams
from tests.fakes import NOW, cfg, make_runner


class FakeTM:
    def __init__(self, avg_r=0.5, fail=None):
        self.avg_r, self.fail, self.calls, self.plans, self.until = avg_r, fail, 0, [], None

    def normalize_plan(self, raw):
        self.plans.append(raw)
        return raw

    def fetch_chart(self, sym):
        if self.fail:
            raise RuntimeError(self.fail)
        return "{}"

    def time_machine(self, text, plan, now, until=None):
        self.calls += 1
        self.until = until
        return {"mc": {"avg_R": self.avg_r, "seed": 19, "replays": 10000, "p_target": 0.3, "p_stop": 0.2, "p_neither": 0.5},
                "history": {"sessions": 59, "triggered": 30, "avg_R": 0.01}, "verdict": "Leans toward the target"}


def gated(tm, on=True):
    r, b, *_ = make_runner(config=cfg(strategy=replace(StrategyParams(), time_machine_gate=on)))
    r.startup()
    r._tm = tm
    return r, b


class TimeMachineGate(unittest.TestCase):
    # BREAKOUT: limit 100.95, stop 100.0 -> round-trip cost 10 bps = 0.106R
    def test_favourable_replay_lets_the_order_through_with_the_plan_the_skill_expects(self):
        tm = FakeTM(avg_r=0.5)
        r, b = gated(tm)
        out = r.tick(NOW)
        self.assertEqual(len(b.submitted), 1)
        self.assertEqual(out["submitted"][0]["time_machine"]["pass"], True)
        p = tm.plans[0]
        self.assertEqual((p["symbol"], p["entry_time"], p["stop_rule"], p["setup"], p["opening_range"]),
                         ("AAA", "09:50", "or_mid", "orb", {"minutes": 15}))
        self.assertEqual(tm.until, "15:55")

    def test_replay_below_cost_blocks_the_order_and_is_not_rerun_every_poll(self):
        tm = FakeTM(avg_r=0.05)
        r, b = gated(tm)
        out = r.tick(NOW)
        r.tick(NOW + timedelta(seconds=10))
        self.assertEqual(b.submitted, [])
        self.assertIn("time machine: avg R +0.050 <= cost 0.106R", out["skipped"][-1][1])
        self.assertEqual(tm.calls, 1)

    def test_simulation_failure_fails_closed(self):
        r, b = gated(FakeTM(fail="Yahoo is rate limiting"))
        out = r.tick(NOW)
        self.assertEqual(b.submitted, [])
        self.assertIn("unavailable", out["skipped"][-1][1])

    def test_gate_off_never_calls_it(self):
        tm = FakeTM(avg_r=-1)
        r, b = gated(tm, on=False)
        r.tick(NOW)
        self.assertEqual((len(b.submitted), tm.calls), (1, 0))


if __name__ == "__main__":
    unittest.main()
