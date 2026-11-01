import json
import random
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta

from papertrader.audit import Audit
from papertrader.backtest import run_backtest, simulate_path, summarize
from papertrader.config import Costs, StrategyParams
from papertrader.evaluate import EvalError, evaluate
from papertrader.strategy import build_levels, find_signal, find_signals, size_qty
from tests.fakes import BREAKOUT, DAY, cfg, et, make_bars

P = StrategyParams()


class Signals(unittest.TestCase):
    def test_breakout_found_with_midpoint_stop(self):
        s = find_signal("AAA", make_bars(DAY, BREAKOUT), DAY, P)
        self.assertEqual((s.or_high, s.or_low, s.stop, s.ref_close), (100.4, 99.6, 100.0, 100.8))
        self.assertEqual(s.bar_end, et(DAY, 9, 50))

    def test_no_look_ahead(self):
        bars = make_bars(DAY, BREAKOUT)
        self.assertIsNone(find_signal("AAA", bars, DAY, P, known_until=et(DAY, 9, 49)))  # breakout bar not complete yet
        self.assertIsNotNone(find_signal("AAA", bars, DAY, P, known_until=et(DAY, 9, 50)))

    def test_missing_bar_makes_chart_unusable(self):
        bars = make_bars(DAY, BREAKOUT)
        del bars[1]
        self.assertIsNone(find_signal("AAA", bars, DAY, P))

    def test_chase_is_skipped_and_later_breakouts_ignored(self):
        rows = BREAKOUT[:3] + [(100.3, 103, 100.2, 102.9), (102.9, 104, 102, 103.5)]
        self.assertIsNone(find_signal("AAA", make_bars(DAY, rows), DAY, P))

    def test_reentry_needs_a_close_back_under_the_high(self):
        # breakout 9:50, stays above (no signal), closes under 10:00, breaks again 10:05
        rows = BREAKOUT + [(100.8, 101, 100.6, 100.9), (100.9, 100.9, 100.1, 100.2), (100.2, 100.9, 100.1, 100.7)]
        bars = make_bars(DAY, rows)
        self.assertEqual([s.bar_end for s in find_signals("AAA", bars, DAY, P)], [et(DAY, 9, 50)])
        two = replace(P, max_entries=2)
        self.assertEqual([s.bar_end for s in find_signals("AAA", bars, DAY, two)], [et(DAY, 9, 50), et(DAY, 10, 5)])
        self.assertEqual(find_signal("AAA", bars, DAY, two, known_until=et(DAY, 10, 0)).bar_end, et(DAY, 9, 50))

    def test_after_entry_cutoff_no_signal(self):
        rows = BREAKOUT[:3] + [(100.0, 100.2, 99.9, 100.1)] * 70 + [(100.2, 101, 100.2, 100.9)]
        self.assertIsNone(find_signal("AAA", make_bars(DAY, rows), DAY, P))

    def test_levels_and_size_caps(self):
        lv = build_levels(100.8, 100.0, P)
        self.assertEqual((lv.limit, lv.stop, lv.target), (100.9, 100.0, 101.8))
        self.assertEqual(size_qty(lv, 50, 5000, 10000, 10000), 49)
        self.assertEqual(size_qty(lv, 50, 5000, 300, 10000), 2)    # cash binds
        self.assertEqual(size_qty(lv, 50, 5000, 10000, 150), 1)    # exposure binds
        self.assertEqual(size_qty(lv, 5, 5000, 10000, 10000), 5)   # per-trade loss binds
        self.assertIsNone(build_levels(100.8, 90.0, P))             # stop too far
        self.assertIsNone(build_levels(4.0, 3.9, P))                # below price floor


def day_rows(after, n_flat=0):
    """BREAKOUT then `after` bars, padded with flat bars so the 15:55 bar exists."""
    rows = list(BREAKOUT) + list(after)
    while len(rows) < 78:
        rows.append((100.8, 100.85, 100.75, 100.8))
    return rows


class FillSimulation(unittest.TestCase):
    def path(self, rows, costs=None, seed=1):
        bars = make_bars(DAY, rows)
        sig = find_signal("AAA", bars, DAY, P)
        lv = build_levels(sig.ref_close, sig.stop, P)
        c = costs or Costs(reject_prob=0, partial_fill_prob=0)
        from papertrader.strategy import day_bars
        return simulate_path(sig, day_bars(bars, DAY), lv, P, c, random.Random(seed))

    def test_target_hit(self):
        pa, _ = self.path(day_rows([(100.8, 102.0, 100.7, 101.9)]))
        self.assertEqual((pa.reason, pa.exit_ref), ("target", 101.8))

    def test_stop_hit_fills_below_stop_with_slippage(self):
        pa, _ = self.path(day_rows([(100.8, 100.9, 99.5, 99.9)]))
        self.assertEqual(pa.reason, "stop")
        self.assertLess(pa.exit_fill, 100.0)

    def test_stop_assumed_first_when_both_touched_in_one_bar(self):
        pa, _ = self.path(day_rows([(100.8, 102.5, 99.0, 101.0)]))
        self.assertEqual(pa.reason, "stop")

    def test_gap_through_stop_fills_at_open(self):
        pa, _ = self.path(day_rows([(100.8, 100.85, 100.75, 100.8), (98.0, 98.5, 97.0, 97.5)]))
        self.assertEqual(pa.reason, "stop")
        self.assertEqual(pa.exit_ref, 98.0)

    def test_time_exit_at_flat_by_bar(self):
        pa, _ = self.path(day_rows([]))
        self.assertEqual(pa.reason, "time")
        self.assertEqual(pa.exit_ts, et(DAY, 15, 55))

    def test_entry_fill_includes_spread_and_slippage_and_is_missed_above_limit(self):
        pa, _ = self.path(day_rows([(100.8, 100.9, 100.7, 100.8)]))
        self.assertGreater(pa.entry_fill, pa.entry_ref)
        pa, why = self.path(day_rows([(101.2, 101.5, 101.0, 101.3)]))
        self.assertEqual(why, "missed_limit")

    def test_random_rejections_and_partial_fills(self):
        _, why = self.path(day_rows([(100.8, 100.9, 100.7, 100.8)]), Costs(reject_prob=1.0))
        self.assertEqual(why, "rejected")
        pa, _ = self.path(day_rows([(100.8, 100.9, 100.7, 100.8)]), Costs(reject_prob=0, partial_fill_prob=1.0))
        self.assertTrue(0.1 <= pa.frac <= 0.9)


def universe_bars(days, win=True):
    out = {"AAA": [], "SPY": []}
    for d in days:
        after = [(100.8, 102.0, 100.7, 101.9)] if win else [(100.8, 100.9, 99.5, 99.9)]
        out["AAA"] += make_bars(d, day_rows(after))
        out["SPY"] += make_bars(d, [(400, 400.5, 399.5, 400)] * 78)
    return out


class BacktestAccounting(unittest.TestCase):
    def days(self, n=6):
        d, out = date(2026, 10, 5), []
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out

    def run_bt(self, win=True, **kw):
        days = self.days()
        c = cfg(universe=["AAA"], costs=Costs(reject_prob=0, partial_fill_prob=0), **kw)
        return days, c, run_backtest(universe_bars(days, win), days, c, P, seed=1)

    def test_net_is_below_gross_by_costs(self):
        days, c, res = self.run_bt()
        s = summarize(res, c.capital_usd)
        self.assertEqual(s["trades"], 6)
        self.assertGreater(s["gross_pnl"], s["net_pnl"])
        self.assertGreater(s["estimated_costs"], 0)

    def test_losses_and_drawdown_reported(self):
        days, c, res = self.run_bt(win=False)
        s = summarize(res, c.capital_usd)
        self.assertLess(s["net_pnl"], 0)
        self.assertGreater(s["max_drawdown_usd"], 0)
        self.assertEqual(s["wins"], 0)
        self.assertLessEqual(s["worst_trade"], -1)

    def test_per_trade_loss_is_bounded_except_for_stop_slippage(self):
        days, c, res = self.run_bt(win=False, max_loss_per_trade_usd=20)
        for t in res["trades"]:
            self.assertLess(-t["net_pnl"], 20 * 1.5)

    def test_settled_cash_rule_limits_reuse_of_proceeds(self):
        days = self.days(3)
        c = cfg(universe=["AAA"], capital_usd=2500, max_order_notional_usd=2500, max_account_exposure_usd=2500,
                max_loss_per_trade_usd=100, max_daily_loss_usd=300, costs=Costs(reject_prob=0, partial_fill_prob=0))
        res = run_backtest(universe_bars(days), days, c, P, seed=1)
        for t in res["trades"]:
            self.assertLessEqual(t["entry_cost"], 2500 + 1e-6)  # never more than the principal in any trade
        self.assertEqual(len(res["trades"]), 3)  # one per day: proceeds settle T+1 and are then reusable

    def test_exposure_and_benchmark_in_summary(self):
        days, c, res = self.run_bt()
        from papertrader.strategy import day_bars
        s = summarize(res, c.capital_usd, universe_bars(days)["SPY"], days)
        self.assertGreater(s["avg_capital_exposure_pct"], 0)
        self.assertIn("benchmark_spy_buy_and_hold", s)
        self.assertIn("p05", s["pnl_distribution"])

    def test_seeded_runs_are_deterministic(self):
        a = run_backtest(universe_bars(self.days()), self.days(), cfg(universe=["AAA"]), P, seed=3)
        b = run_backtest(universe_bars(self.days()), self.days(), cfg(universe=["AAA"]), P, seed=3)
        self.assertEqual([t["net_pnl"] for t in a["trades"]], [t["net_pnl"] for t in b["trades"]])


class EvaluationSplits(unittest.TestCase):
    def test_dates_must_be_ordered_and_set(self):
        c = cfg(universe=["AAA"])
        with self.assertRaises(EvalError):
            evaluate({}, [], [], c, tempfile.mkdtemp())

    def test_holdout_lock_refuses_retuning(self):
        d, days = date(2026, 1, 5), []
        while len(days) < 60:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        c = cfg(universe=["AAA"], costs=Costs(reject_prob=0, partial_fill_prob=0))
        c.evaluation.data_start, c.evaluation.dev_end = str(days[0]), str(days[29])
        c.evaluation.walk_forward_end, c.evaluation.holdout_end = str(days[44]), str(days[-1])
        c.evaluation.min_trades_to_select, c.evaluation.wf_test_days = 5, 5
        c.evaluation.param_grid = {"target_r": [1.0, 1.5]}
        bars = universe_bars(days)
        rd = tempfile.mkdtemp()
        out = evaluate(bars, bars["SPY"], days, c, rd)
        self.assertIn("result", out["holdout"])
        self.assertIn("folds", out["walk_forward"])
        evaluate(bars, bars["SPY"], days, c, rd)  # same parameters: allowed
        lock = json.loads((__import__("pathlib").Path(rd) / "holdout_lock.json").read_text())
        lock["params_sha256"] = "different"
        (__import__("pathlib").Path(rd) / "holdout_lock.json").write_text(json.dumps(lock))
        with self.assertRaises(EvalError):
            evaluate(bars, bars["SPY"], days, c, rd)


class AuditRedaction(unittest.TestCase):
    def test_secrets_never_reach_disk(self):
        d = tempfile.mkdtemp()
        a = Audit(d, secrets=["PKABCDEFGHIJ", "s3cretvalue"], echo=False)
        a.log("x", api_key="PKABCDEFGHIJ", nested={"secret_key": "s3cretvalue", "msg": "key PKABCDEFGHIJ leaked in text"})
        a.alert("boom s3cretvalue", Authorization="Bearer abc")
        a.journal({"note": "s3cretvalue"})
        blob = "".join(p.read_text() for p in __import__("pathlib").Path(d).iterdir())
        for s in ("PKABCDEFGHIJ", "s3cretvalue", "Bearer abc"):
            self.assertNotIn(s, blob)
        self.assertIn("[REDACTED]", blob)


if __name__ == "__main__":
    unittest.main()
