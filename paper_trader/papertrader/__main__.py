"""CLI:  python -m papertrader <verify | dry-run | paper-run | stop | clear-kill | backtest | report>"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .audit import Audit
from .config import load_config
from .guards import SafetyError, ensure_restrictions, load_credentials, verify_paper_account, PAPER_TRADING_URL
from .state import StateStore
from .timeutil import ET, UTC

BASE = Path(__file__).resolve().parent.parent
STATE = BASE / "state"
KILL = STATE / "KILL"


def _mask(num):
    return "***" + str(num)[-4:]


def _connect():
    from .alpaca_adapter import AlpacaIexData, AlpacaPaperBroker
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    return AlpacaPaperBroker(key, secret), AlpacaIexData(key, secret), (key, secret)


def _cfg(args):
    path = Path(args.config) if args.config else BASE / "config.json"
    if not path.exists():
        raise SafetyError("%s not found. Copy config.template.json to config.json and fill in your limits." % path)
    return load_config(path)


def cmd_verify(args):
    """Read-only: credentials, paper proof, restrictions, clock, market data. Writes nothing."""
    broker, data, _ = _connect()
    print("trading endpoint (hard-coded):", PAPER_TRADING_URL)
    acct = broker.get_account()
    verify_paper_account(acct)
    print("paper account proven: %s status=%s cash=%s equity=%s" % (_mask(acct["account_number"]), acct["status"], acct["cash"], acct["equity"]))
    try:
        waived = _cfg(args).accept_unrestricted_options_level
    except SafetyError:
        waived = False
    problems, _ = ensure_restrictions(broker, apply=False, waive_options=waived)
    print("restrictions (long-only, 1x margin, options 0%s):" % (" WAIVED by user" if waived else ""),
          "VERIFIED" if not problems else "NOT VERIFIED")
    for p in problems:
        print("   -", p)
    if problems:
        print("   `paper-run` will set no_shorting/1x/options-0 on this PAPER account and re-verify; it refuses to start if it cannot.")
    clock = broker.get_clock()
    print("market open:", clock["is_open"], "| next open:", clock["next_open"])
    now = datetime.now(UTC)
    bars = data.get_bars(["SPY"], now - timedelta(days=5), now - timedelta(minutes=20))["SPY"]
    quote = data.get_latest_quotes(["SPY"])["SPY"]
    print("market data (IEX): %d SPY 5-min bars in last 5 days, last bar %s; quote bid/ask %s/%s at %s" % (
        len(bars), bars[-1].ts if bars else None, quote["bid"], quote["ask"], quote["ts"]))
    try:
        cfg = _cfg(args)
        print("config problems:", cfg.submission_problems() or "none")
    except SafetyError as e:
        print("config:", e)
    return 0


def _runner(args, submit, replay=False):
    from .hold import HoldRunner
    from .runner import Runner
    cfg = _cfg(args)
    if cfg.hold.enabled and replay:
        raise SafetyError("--replay is not supported in hold mode")
    broker, data, secrets = _connect()
    audit = Audit(STATE, secrets=secrets)
    r = (HoldRunner if cfg.hold.enabled else Runner)(
        cfg, broker, data, audit, StateStore(STATE / "state.json"), KILL, submit=submit, replay=replay)
    return r, cfg


def cmd_dry(args):
    replay = bool(args.replay)
    r, cfg = _runner(args, submit=False, replay=replay)
    r.startup()
    print("DRY-RUN: tickets are shown, NO orders are sent. Config problems:", cfg.problems() or "none")
    if cfg.problems():
        print("Idle: set the missing values in config.json first. Nothing to propose.")
        return 0
    if args.once or replay:
        now = datetime.strptime(args.replay, "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(UTC) if replay else datetime.now(UTC)
        out = r.tick(now)
        print(json.dumps(out, indent=1, default=str))
        if out.get("idle") == "market closed":
            print("Market is closed. Use --replay 'YYYY-MM-DD HH:MM' (ET) to see tickets for a past session.")
        return 0
    r.run()
    return 0


def _stay_awake():
    """Windows: ask the OS not to sleep or blank the screen (Modern Standby) while this process runs.
    Released when the process exits; changes no setting. A closed lid or a manual Sleep still sleeps."""
    if sys.platform == "win32":
        import ctypes
        es_continuous, es_system_required, es_display_required = 0x80000000, 0x1, 0x2
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required | es_display_required)


def cmd_run(args):
    _stay_awake()
    r, cfg = _runner(args, submit=True)
    print("PAPER-RUN: automated orders go to the Alpaca PAPER account only (%s). Ctrl+C or `stop` to halt." % PAPER_TRADING_URL)
    r.run()
    return 0


def cmd_stop(args):
    STATE.mkdir(parents=True, exist_ok=True)
    KILL.write_text(datetime.now(UTC).isoformat())
    print("Kill switch set. The runner stops opening positions within one poll and cancels unfilled entries. "
          "Open positions and their stop/target orders are left in place. `clear-kill` before restarting.")
    return 0


def cmd_clear(args):
    KILL.unlink(missing_ok=True)
    print("Kill switch cleared.")
    return 0


def cmd_status(args):
    from .monitor import status_text
    broker, _, _ = _connect()
    print(status_text(broker, _cfg(args), STATE / "audit.jsonl"))
    return 0


def cmd_watch(args):
    from .monitor import status_text, watch
    broker, _, _ = _connect()
    cfg = _cfg(args)
    print("woke:", watch(broker, STATE / "audit.jsonl", args.max_minutes))
    print(status_text(broker, cfg, STATE / "audit.jsonl"))
    return 0


def cmd_backtest(args):
    from .alpaca_adapter import AlpacaIexData
    from .evaluate import evaluate
    from .history import fetch_history, trading_days
    cfg = _cfg(args)
    if cfg.problems():
        raise SafetyError("; ".join(cfg.problems()))
    key, secret = load_credentials(dotenv_path=BASE / ".env")
    data = AlpacaIexData(key, secret)
    ev = cfg.evaluation
    start, end = datetime.fromisoformat(ev.data_start).date(), datetime.fromisoformat(ev.holdout_end).date()
    syms = sorted(set(cfg.universe) | {"SPY"})
    bars = fetch_history(data, syms, start, end, STATE / "cache")
    days = trading_days({"SPY": bars["SPY"]}, start, end)
    out = evaluate(bars, bars["SPY"], days, cfg, BASE / "reports")
    print(json.dumps({k: out[k] for k in out if k != "assumptions"}, indent=1, default=str)[:6000])
    print("full report: %s" % (BASE / "reports" / "backtest_report.json"))
    return 0


def cmd_report(args):
    from .evaluate import report_forward
    cfg = _cfg(args)
    print(json.dumps(report_forward(STATE / "trades.jsonl", cfg.capital_usd), indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="papertrader", description=__doc__)
    ap.add_argument("--config", help="path to config.json (default: paper_trader/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("verify", help="read-only checks of credentials, paper account, restrictions, data")
    d = sub.add_parser("dry-run", help="fetch data and print proposed tickets; never sends orders")
    d.add_argument("--once", action="store_true", help="one pass then exit")
    d.add_argument("--replay", metavar="'YYYY-MM-DD HH:MM'", help="evaluate a past session moment (ET)")
    sub.add_parser("paper-run", help="AUTOMATED paper orders (needs complete config + verified restrictions)")
    sub.add_parser("stop", help="kill switch: stop new positions, cancel unfilled entries, leave positions")
    sub.add_parser("clear-kill", help="remove the kill switch file")
    sub.add_parser("backtest", help="development / walk-forward / holdout evaluation")
    sub.add_parser("report", help="paper-forward report from the runner's trade journal")
    sub.add_parser("status", help="read-only: day P&L, open positions, today's orders, runner heartbeat")
    w = sub.add_parser("watch", help="read-only: wait for an order/exit/alert/position change, then print status")
    w.add_argument("--max-minutes", type=int, default=30)
    args = ap.parse_args(argv)
    fn = {"verify": cmd_verify, "dry-run": cmd_dry, "paper-run": cmd_run, "stop": cmd_stop, "clear-kill": cmd_clear,
          "backtest": cmd_backtest, "report": cmd_report, "status": cmd_status, "watch": cmd_watch}[args.cmd]
    try:
        return fn(args)
    except SafetyError as e:
        print("REFUSED (fail closed): %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
