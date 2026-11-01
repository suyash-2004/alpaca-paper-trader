import unittest
from datetime import timedelta
from decimal import Decimal as D

from papertrader.risk import Intent, RiskContext, check_entry
from papertrader.ledger import CashState
from papertrader.strategy import Levels
from tests.fakes import BREAKOUT, DAY, NOW, FakeBroker, FakeData, cfg, et, make_bars, make_runner
from papertrader.timeutil import UTC


def ready(**kw):
    r, b, d, tmp = make_runner(**kw)
    r.startup()
    return r, b, d, tmp


class SubmitPath(unittest.TestCase):
    def test_signal_becomes_bracket_buy_limited_to_cash_and_risk_caps(self):
        r, b, *_ = ready()
        out = r.tick(NOW)
        self.assertEqual(len(b.submitted), 1)
        s = b.submitted[0]
        self.assertEqual((s["symbol"], s["limit"], s["sl"]), ("AAA", 100.95, 100.0))  # ask 100.85 + 10bps
        self.assertLessEqual(s["qty"] * s["limit"], 5000)   # max_order_notional
        self.assertLessEqual(s["qty"] * (s["limit"] - s["sl"]), 50)  # max loss per trade
        self.assertEqual(out["submitted"][0]["client_order_id"], "ptr-e-20261027-AAA-0950")

    def test_duplicate_order_protection_same_run(self):
        r, b, *_ = ready()
        r.tick(NOW)
        r.tick(NOW + timedelta(seconds=10))
        self.assertEqual(len(b.submitted), 1)

    def test_duplicate_order_protection_survives_restart(self):
        r, b, d, tmp = ready()
        r.tick(NOW)
        b.open.clear()  # even if the broker forgot about it, the persisted id blocks a resend
        r2, b2, *_ = make_runner(broker=b, data=d)
        r2.state.path = r.state.path
        r2.state.data = type(r.state)(r.state.path).data
        r2.audit, r2.kill_path = r.audit, r.kill_path
        r2.startup()
        r2.tick(NOW + timedelta(seconds=10))
        self.assertEqual(len(b.submitted), 1)

    def test_same_symbol_not_ordered_twice_even_with_new_id(self):
        r, b, *_ = ready()
        r.tick(NOW)
        r.state.data["sent_ids"].clear()
        r._proposed.clear()
        r.tick(NOW + timedelta(seconds=10))
        self.assertEqual(len(b.submitted), 1)  # open buy order for AAA already exists

    def test_rejected_order_is_logged_and_not_retried(self):
        r, b, *_ = ready()
        b.fail_submit = True
        r.tick(NOW)
        b.fail_submit = False
        r.tick(NOW + timedelta(seconds=10))
        self.assertEqual(b.submitted, [])
        self.assertIn("order rejected", (r.audit.dir / "alerts.log").read_text())

    def test_dry_run_never_submits(self):
        r, b, *_ = ready(submit=False)
        out = r.tick(NOW)
        self.assertEqual(b.submitted, [])
        self.assertEqual(len(out["proposed"]), 1)
        self.assertEqual(r.state.data["sent_ids"], [])
        self.assertEqual(b.cfg_changes, [])  # dry-run does not even change account settings

    def test_replay_can_never_submit(self):
        with self.assertRaises(Exception):
            make_runner(submit=True, replay=True)


class CashCapEnforcedBeforeBroker(unittest.TestCase):
    def test_small_settled_cash_shrinks_size_never_exceeds_it(self):
        r, b, *_ = ready(broker=FakeBroker(cash="1000"))
        r.tick(NOW)
        s = b.submitted[0]
        self.assertLessEqual(s["qty"] * s["limit"], 1000)

    def test_zero_settled_cash_submits_nothing(self):
        r, b, *_ = ready(broker=FakeBroker(cash="0"))
        r.tick(NOW)
        self.assertEqual(b.submitted, [])

    def test_unsettled_sale_proceeds_are_not_spendable(self):
        from tests.test_ledger import sell
        b = FakeBroker(cash="1000")
        b.closed.append(sell(DAY, "10", "99", "s1"))  # 990 of the 1000 cash came from today's sale
        r, b, *_ = ready(broker=b)
        r.tick(NOW)
        s = b.submitted[0] if b.submitted else None
        self.assertTrue(s is None or s["qty"] * s["limit"] <= 10)

    def test_existing_open_buy_order_reserved(self):
        b = FakeBroker(cash="3050")
        b.open.append({"id": "x", "client_order_id": "ptr-e-old", "symbol": "ZZZ", "side": "buy", "status": "new", "qty": "29",
                       "filled_qty": "0", "limit_price": "100", "created_at": NOW, "legs": []})
        r, b, *_ = ready(broker=b)
        r.tick(NOW)
        self.assertEqual(b.submitted[0]["qty"], 1)  # 3050 - 2900 reserved = 150 -> one share at 100.95

    def test_oversized_order_rejected_locally_without_calling_alpaca(self):
        ctx = RiskContext(cfg=cfg(), cash=CashState(available=D(500), capital=D(10000)))
        it = Intent("AAA", 10, Levels(100.9, 100.0, 101.8), "ptr-e-1")  # $1,009 notional > $500 available
        v = check_entry(it, ctx)
        self.assertTrue(any("exceeds available settled cash" in x for x in v))

    def test_cash_unknown_blocks(self):
        ctx = RiskContext(cfg=cfg(), cash=CashState(ok=False, reasons=["x"], capital=D(10000)))
        v = check_entry(Intent("AAA", 1, Levels(100.9, 100.0, 101.8), "i"), ctx)
        self.assertTrue(any("settled cash unknown" in x for x in v))


class RiskLimits(unittest.TestCase):
    def ctx(self, **kw):
        base = dict(cfg=cfg(), cash=CashState(available=D(10000), capital=D(10000)))
        base.update(kw)
        return RiskContext(**base)

    def intent(self, qty=10, **kw):
        return Intent("AAA", qty, Levels(100.9, 100.0, 101.8), kw.get("cid", "ptr-e-1"))

    def test_clean_order_passes(self):
        self.assertEqual(check_entry(self.intent(), self.ctx()), [])

    def test_per_trade_loss_limit(self):
        self.assertTrue(any("max_loss_per_trade" in x for x in check_entry(self.intent(60), self.ctx())))  # 60*0.9=54>50

    def test_max_order_notional(self):
        c = cfg(max_order_notional_usd=500)
        self.assertTrue(any("max_order_notional" in x for x in check_entry(self.intent(10), self.ctx(cfg=c))))

    def test_max_exposure(self):
        cash = CashState(available=D(10000), capital=D(10000), open_cost=D(7500))
        self.assertTrue(any("max_account_exposure" in x for x in check_entry(self.intent(10), self.ctx(cash=cash))))

    def test_max_open_positions(self):
        pos = [{"symbol": s} for s in ("A", "B", "C")]
        self.assertTrue(any("max_open_positions" in x for x in check_entry(self.intent(), self.ctx(positions=pos))))

    def test_daily_loss_limit_halts_new_entries(self):
        v = check_entry(self.intent(), self.ctx(day_pnl=D(-150)))
        self.assertTrue(any("daily loss limit" in x for x in v))
        v = check_entry(self.intent(50), self.ctx(day_pnl=D(-120)))  # -120 + 45 > 150? 165 > 150
        self.assertTrue(any("max_daily_loss" in x for x in v))

    def test_kill_switch_and_stale_data_and_foreign_and_duplicate(self):
        self.assertTrue(any("kill" in x for x in check_entry(self.intent(), self.ctx(kill=True))))
        self.assertTrue(any("stale" in x for x in check_entry(self.intent(), self.ctx(data_ok=False))))
        self.assertTrue(any("outside this runner" in x for x in check_entry(self.intent(), self.ctx(foreign_symbols=frozenset({"AAA"})))))
        self.assertTrue(any("duplicate" in x for x in check_entry(self.intent(), self.ctx(seen_ids=frozenset({"ptr-e-1"})))))

    def test_incomplete_config_blocks_everything(self):
        v = check_entry(self.intent(), self.ctx(cfg=cfg(capital_usd=None)))
        self.assertTrue(v and "config incomplete" in v[0])

    def test_runner_stops_opening_after_daily_loss_hit(self):
        r, b, *_ = ready(broker=FakeBroker(equity="9840", last_equity="10000"))
        out = r.tick(NOW)
        self.assertEqual(b.submitted, [])
        self.assertIn("daily loss", out["halted"])
        r.tick(NOW + timedelta(seconds=10))  # latched for the day
        self.assertEqual(b.submitted, [])

    def test_max_open_positions_enforced_across_signals_in_one_tick(self):
        both = {s: make_bars(DAY, BREAKOUT) for s in ("AAA", "BBB")}
        r, b, *_ = ready(data=FakeData(both), config=cfg(max_open_positions=1))
        r.tick(NOW)
        self.assertEqual(len(b.submitted), 1)


class StaleData(unittest.TestCase):
    def test_stale_bars_block_entries(self):
        r, b, *_ = ready()
        out = r.tick(NOW + timedelta(minutes=10))
        self.assertEqual(b.submitted, [])
        self.assertIn("stale data", out["skipped"])

    def test_stale_quote_blocks_entry(self):
        r, b, d, _ = ready()
        d.get_latest_quotes = lambda syms: {s: {"bid": 100.8, "ask": 100.85, "ts": NOW - timedelta(minutes=5)} for s in syms}
        out = r.tick(NOW)
        self.assertEqual(b.submitted, [])
        self.assertTrue(any("stale" in w for _, w in out["skipped"]))

    def test_wide_spread_blocks_entry(self):
        r, b, d, _ = ready()
        d.get_latest_quotes = lambda syms: {s: {"bid": 99.0, "ask": 101.0, "ts": NOW} for s in syms}
        r.tick(NOW)
        self.assertEqual(b.submitted, [])

    def test_data_outage_halts_entries_and_alerts_but_loop_survives(self):
        r, b, d, _ = ready()
        d.fail = True
        for i in range(3):
            r.tick(NOW + timedelta(seconds=10 * i))
        self.assertEqual(b.submitted, [])
        self.assertIn("market data failing", (r.audit.dir / "alerts.log").read_text())
        d.fail = False
        r.tick(NOW + timedelta(seconds=40))
        self.assertEqual(len(b.submitted), 1)  # recovers when data returns

    def test_old_signal_is_not_chased(self):
        r, b, *_ = ready()
        r.tick(NOW + timedelta(minutes=5))  # signal bar closed 5 min ago
        self.assertEqual(b.submitted, [])


class KillSwitchAndShutdown(unittest.TestCase):
    def test_kill_switch_stops_entries_cancels_unfilled_entries_but_never_sells(self):
        r, b, *_ = ready()
        r.tick(NOW)
        r.kill_path.write_text("x")
        b.positions.append({"symbol": "HELD", "qty": "5", "avg_entry_price": "10", "cost_basis": "50", "market_value": "50"})
        out = r.tick(NOW + timedelta(seconds=10))
        self.assertTrue(out["stopped"])
        self.assertEqual(b.sells, [])
        self.assertEqual(len(b.canceled), 1)           # only the unfilled entry parent
        self.assertTrue(b.canceled[0].startswith("o"))  # not a protective leg
        self.assertIn("NOT liquidated", (r.audit.dir / "alerts.log").read_text())

    def test_start_refused_while_kill_switch_exists(self):
        r, b, *_ = make_runner()
        r.kill_path.write_text("x")
        from papertrader.guards import SafetyError
        with self.assertRaises(SafetyError):
            r.startup()

    def test_filled_entries_are_not_canceled_on_shutdown(self):
        r, b, *_ = ready()
        r.tick(NOW)
        b.open[0]["filled_qty"] = "10"
        r.shutdown("test")
        self.assertEqual(b.canceled, [])


class PositionsReconcileAndExit(unittest.TestCase):
    def pos(self, sym="AAA", qty="10"):
        return {"symbol": sym, "qty": qty, "avg_entry_price": "100.9", "cost_basis": "1009", "market_value": "1009"}

    def test_unexpected_position_alerts_and_is_never_touched(self):
        b = FakeBroker()
        b.positions.append(self.pos("ZZZ"))
        r, b, *_ = ready(broker=b)
        r.tick(et_flat())
        self.assertEqual(b.sells, [])
        self.assertIn("unexpected position", (r.audit.dir / "alerts.log").read_text())

    def test_unexpected_open_order_alerts(self):
        b = FakeBroker()
        b.open.append({"id": "m1", "client_order_id": "manual-1", "symbol": "ZZZ", "side": "sell", "status": "new", "legs": []})
        r, *_ = ready(broker=b)
        self.assertIn("unexpected open order", (r.audit.dir / "alerts.log").read_text())

    def test_flat_by_exit_sells_only_runner_owned_positions_after_cancelling_legs(self):
        r, b, *_ = ready()
        r.tick(NOW)
        b.positions += [self.pos("AAA", "10"), self.pos("FOREIGN", "7")]
        r.state.data["foreign"].append("FOREIGN")
        r.tick(et_flat())
        self.assertEqual([s["symbol"] for s in b.sells], ["AAA"])
        self.assertEqual(b.sells[0]["qty"], 10)
        self.assertTrue({"l1-1", "l2-1"} <= set(b.canceled))

    def test_dry_run_flat_by_does_not_sell(self):
        r, b, *_ = ready(submit=False)
        r.state.data["plans"]["AAA"] = {"stop": 100}
        b.positions.append(self.pos())
        r.tick(et_flat())
        self.assertEqual(b.sells, [])

    def test_position_without_protective_order_alerts_but_is_not_auto_fixed(self):
        r, b, *_ = ready()
        r.state.data["plans"]["AAA"] = {"stop": 100}
        b.positions.append(self.pos())
        r.tick(NOW)
        self.assertIn("no protective sell order", (r.audit.dir / "alerts.log").read_text())
        self.assertEqual(b.sells, [])

    def test_market_closed_is_idle(self):
        r, b, *_ = ready()
        b.is_open = False
        self.assertEqual(r.tick(NOW)["idle"], "market closed")
        self.assertEqual(b.submitted, [])


def et_flat():
    return et(DAY, 15, 56).astimezone(UTC)


if __name__ == "__main__":
    unittest.main()
