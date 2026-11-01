import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from papertrader import guards
from papertrader.config import ConfigError, load_config
from papertrader.guards import SafetyError
from tests.fakes import FakeBroker, make_runner, cfg

GOOD = {"ALPACA_PAPER_API_KEY": "PKABCDEFGHIJ", "ALPACA_PAPER_SECRET_KEY": "s3cretvalue"}


class Endpoint(unittest.TestCase):
    def test_paper_url_is_hardcoded_constant(self):
        self.assertEqual(guards.PAPER_TRADING_URL, "https://paper-api.alpaca.markets")

    def test_live_endpoint_rejected(self):
        for url in ("https://api.alpaca.markets", "https://paper-api.alpaca.markets.evil.com", ""):
            with self.assertRaises(SafetyError):
                guards.assert_paper_endpoint(url)
        guards.assert_paper_endpoint("https://paper-api.alpaca.markets/")
        guards.assert_paper_endpoint("https://paper-api.alpaca.markets/v2")
        with self.assertRaises(SafetyError):
            guards.assert_paper_endpoint("https://api.alpaca.markets/v2")
        guards.load_credentials({**GOOD, "APCA_API_BASE_URL": "https://paper-api.alpaca.markets/v2"})

    def test_env_pointing_at_live_url_refuses(self):
        for name in ("APCA_API_BASE_URL", "ALPACA_API_BASE_URL"):
            with self.assertRaises(SafetyError):
                guards.load_credentials({**GOOD, name: "https://api.alpaca.markets"})
        guards.load_credentials({**GOOD, "APCA_API_BASE_URL": "https://paper-api.alpaca.markets"})

    def test_credentials_missing_or_live_looking_refuse(self):
        with self.assertRaises(SafetyError):
            guards.load_credentials({})
        with self.assertRaises(SafetyError):
            guards.load_credentials({**GOOD, "ALPACA_PAPER_API_KEY": "AKLIVEKEY12345"})
        self.assertEqual(guards.load_credentials(GOOD)[0], "PKABCDEFGHIJ")

    def test_dotenv_fallback_env_wins_and_live_url_in_file_refuses(self):
        d = Path(tempfile.mkdtemp())
        f = d / ".env"
        f.write_text('# c\nALPACA_PAPER_API_KEY="PKFROMFILE123"\nALPACA_PAPER_SECRET_KEY=filesecret\nOTHER=x\n')
        self.assertEqual(guards.load_credentials({}, f), ("PKFROMFILE123", "filesecret"))
        self.assertEqual(guards.load_credentials(GOOD, f)[0], "PKABCDEFGHIJ")
        f.write_text("ALPACA_PAPER_API_KEY=AKLIVE1234567\nALPACA_PAPER_SECRET_KEY=x\n")
        with self.assertRaises(SafetyError):
            guards.load_credentials({}, f)
        f.write_text("ALPACA_PAPER_API_KEY=PKFROMFILE123\nALPACA_PAPER_SECRET_KEY=x\nAPCA_API_BASE_URL=https://api.alpaca.markets\n")
        with self.assertRaises(SafetyError):
            guards.load_credentials({}, f)

    def test_error_message_never_contains_secret(self):
        try:
            guards.load_credentials({"ALPACA_PAPER_API_KEY": "AKLIVEKEY12345", "ALPACA_PAPER_SECRET_KEY": "topsecret99"})
        except SafetyError as e:
            self.assertNotIn("topsecret99", str(e))
            self.assertNotIn("AKLIVEKEY12345", str(e))

    def test_adapter_builds_paper_client_and_rejects_a_live_one(self):
        from papertrader.alpaca_adapter import AlpacaPaperBroker
        b = AlpacaPaperBroker("PKABCDEFGHIJ", "x")
        self.assertEqual(guards.normalize_url(b._c._base_url), guards.PAPER_TRADING_URL)

        class LiveStub:
            def __init__(self, *a, **k):
                self._base_url = "https://api.alpaca.markets"
        with mock.patch("alpaca.trading.client.TradingClient", LiveStub):
            with self.assertRaises(SafetyError):
                AlpacaPaperBroker("PKABCDEFGHIJ", "x")

    def test_no_live_url_or_switch_in_source(self):
        src = "".join(p.read_text(encoding="utf-8") for p in Path(guards.__file__).parent.glob("*.py"))
        self.assertNotIn("//api.alpaca.markets", src)
        self.assertNotIn("paper=False", src)
        self.assertNotIn("url_override=", src)
        self.assertNotIn("TRADING_LIVE", src)


class AccountProof(unittest.TestCase):
    def test_live_looking_account_number_refused(self):
        a = FakeBroker().get_account()
        guards.verify_paper_account(a)
        for bad in ({"account_number": "123456789"}, {"status": "ACCOUNT_UPDATED"}, {"currency": "EUR"},
                    {"trading_blocked": True}, {"account_blocked": True}):
            with self.assertRaises(SafetyError):
                guards.verify_paper_account({**a, **bad})

    def test_runner_startup_fails_closed_on_non_paper_account(self):
        r, b, *_ = make_runner()
        b.acct["account_number"] = "987654321"
        with self.assertRaises(SafetyError):
            r.startup()
        self.assertEqual(b.submitted, [])


class Restrictions(unittest.TestCase):
    def test_applied_and_verified(self):
        b = FakeBroker()
        b.config.update(no_shorting=False, max_margin_multiplier="4", max_options_trading_level=3)
        b.acct.update(multiplier="1")
        problems, changed = guards.ensure_restrictions(b, apply=True)
        self.assertEqual(problems, [])
        self.assertEqual(changed, {"no_shorting": True, "max_margin_multiplier": "1", "max_options_trading_level": 0})

    def test_verify_only_does_not_change_anything(self):
        b = FakeBroker()
        b.config["no_shorting"] = False
        problems, changed = guards.ensure_restrictions(b, apply=False)
        self.assertTrue(problems)
        self.assertEqual(b.cfg_changes, [])

    def test_unverifiable_or_unchangeable_blocks_submission(self):
        r, b, *_ = make_runner()
        b.ignore_config_changes = True
        b.config["max_margin_multiplier"] = "2"
        with self.assertRaises(SafetyError):
            r.startup()
        r2, b2, *_ = make_runner()
        del b2.config["max_options_trading_level"]
        b2.ignore_config_changes = True
        with self.assertRaises(SafetyError):
            r2.startup()

    def test_margin_account_multiplier_blocks(self):
        b = FakeBroker()
        b.acct["multiplier"] = "4"
        self.assertTrue(guards.restriction_problems(b.get_configurations(), b.get_account()))

    def test_options_waiver_skips_only_the_options_checks(self):
        b = FakeBroker()  # like the real paper account: options level 3, cap not reported, 4x margin, shorting on
        b.config.update(no_shorting=False, max_margin_multiplier="4")
        b.config.pop("max_options_trading_level")
        b.acct.update(multiplier="1", options_trading_level=3)
        self.assertTrue(guards.restriction_problems(b.get_configurations(), b.get_account()))  # blocked without it
        problems, changed = guards.ensure_restrictions(b, apply=True, waive_options=True)
        self.assertEqual(problems, [])
        self.assertEqual(changed, {"no_shorting": True, "max_margin_multiplier": "1"})  # never asks for the options change
        for key, bad in (("multiplier", "4"), ("shorting_enabled", True)):
            b2 = FakeBroker()
            b2.acct[key] = bad
            self.assertTrue(guards.restriction_problems(b2.get_configurations(), b2.get_account(), waive_options=True), key)
        b3 = FakeBroker()
        b3.config["no_shorting"] = False
        self.assertTrue(guards.restriction_problems(b3.get_configurations(), b3.get_account(), waive_options=True))

    def test_runner_starts_under_the_waiver_and_logs_it_but_not_without(self):
        def broker():
            b = FakeBroker()
            b.config.pop("max_options_trading_level")
            b.acct["options_trading_level"] = 3
            return b
        r, b, _, tmp = make_runner(broker=broker(), config=cfg(accept_unrestricted_options_level=True))
        r.startup()
        self.assertTrue(all("max_options_trading_level" not in c for c in b.cfg_changes))
        self.assertIn("options_level_check_waived", (Path(tmp) / "audit.jsonl").read_text())
        r2, *_ = make_runner(broker=broker())
        r2.broker.ignore_config_changes = True
        with self.assertRaises(SafetyError):
            r2.startup()

    def test_waiver_flag_must_be_a_real_boolean(self):
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps({"accept_unrestricted_options_level": "yes"}))
        with self.assertRaises(ConfigError):
            load_config(p)


class Liveness(unittest.TestCase):
    def test_every_alpaca_http_call_gets_a_default_timeout(self):
        from requests import Session
        from papertrader import alpaca_adapter as ad
        with mock.patch.object(Session, "request") as req:
            for client in (ad.AlpacaPaperBroker("PKTESTKEY1234", "secret")._c, ad.AlpacaIexData("PKTESTKEY1234", "secret")._c):
                client._session.request("GET", "https://example.invalid/v2/clock")
            ad.AlpacaPaperBroker("PKTESTKEY1234", "secret")._c._session.request("GET", "u", timeout=1)
        self.assertEqual([c.kwargs["timeout"] for c in req.call_args_list], [ad.HTTP_TIMEOUT, ad.HTTP_TIMEOUT, 1])

    def test_run_loop_writes_a_heartbeat(self):
        r, b, _, tmp = make_runner(submit=False)
        r.kill_path.write_text("x")  # one tick, then the kill switch stops the loop
        r.run()
        self.assertIn('"event": "heartbeat"', (Path(tmp) / "audit.jsonl").read_text())


class NoOptionsPath(unittest.TestCase):
    def test_adapter_surface_is_only_equity_reads_and_three_order_calls(self):
        from papertrader.alpaca_adapter import AlpacaPaperBroker
        public = {m for m in dir(AlpacaPaperBroker) if not m.startswith("_")}
        self.assertEqual(public, {"get_account", "get_configurations", "get_clock", "get_trading_days", "get_positions",
                                  "get_orders", "get_asset", "set_configurations", "submit_bracket_buy",
                                  "submit_limit_buy", "submit_market_sell", "cancel_order"})

    def test_option_contract_symbols_are_rejected_before_any_order(self):
        from papertrader.config import HoldCfg
        occ = "SPY261030C00770000"
        self.assertTrue(cfg(hold=HoldCfg(enabled=True, symbol=occ)).problems())
        self.assertTrue(cfg(universe=[occ]).problems())


class ConfigTests(unittest.TestCase):
    def _write(self, d):
        p = Path(tempfile.mkdtemp()) / "c.json"
        p.write_text(json.dumps(d))
        return p

    def test_template_has_required_values_unset_and_submission_off(self):
        c = load_config(Path(__file__).resolve().parent.parent / "config.template.json")
        self.assertFalse(c.order_submission_enabled)
        self.assertEqual(set(c.missing()), {
            "capital_usd", "max_loss_per_trade_usd", "max_daily_loss_usd", "max_open_positions",
            "max_order_notional_usd", "max_account_exposure_usd", "universe"})
        self.assertTrue(c.submission_problems())

    def test_endpoint_and_mode_keys_do_not_exist(self):
        for k in ("endpoint", "base_url", "live", "paper", "margin", "allow_short"):
            with self.assertRaises(ConfigError):
                load_config(self._write({k: True}))

    def test_cannot_exceed_principal(self):
        c = cfg(max_order_notional_usd=20000)
        self.assertTrue(any("exceeds capital" in p for p in c.problems()))
        c = cfg(max_account_exposure_usd=10001)
        self.assertTrue(any("exceeds capital" in p for p in c.problems()))

    def test_submission_requires_explicit_flag_and_complete_limits(self):
        self.assertTrue(cfg(order_submission_enabled=False).submission_problems())
        self.assertEqual(cfg().submission_problems(), [])
        self.assertTrue(cfg(max_open_positions=None).submission_problems())

    def test_runner_refuses_to_start_submission_with_missing_values(self):
        r, b, *_ = make_runner(config=cfg(max_daily_loss_usd=None))
        with self.assertRaises(SafetyError):
            r.startup()


if __name__ == "__main__":
    unittest.main()
