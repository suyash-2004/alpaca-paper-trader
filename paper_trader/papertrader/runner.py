"""The automation loop. Dry-run proposes tickets; paper-run submits them to the PAPER account.

Shutdown / failure behaviour (also in README):
- kill switch, Ctrl+C, fatal error: stop opening positions, cancel UNFILLED entry orders only.
  Filled positions and their broker-side stop/target legs are left alone. Nothing is
  liquidated. Positions found without a protective sell order raise an ALERT.
- process crash: bracket legs live at Alpaca and keep protecting positions for the day. On the
  next start, positions this runner did not record are treated as foreign and never touched.
- market-data failure: new entries halt; broker-side legs and the scheduled flat-by exit
  (which needs no market data) keep working.
- the ONLY automatic sale is the strategy's own flat-by exit of positions this runner opened."""
import time as _time
from datetime import datetime, timedelta
from decimal import Decimal

from .audit import Audit
from .config import Config
from .guards import SafetyError, ensure_restrictions, verify_paper_account
from .journal import ENTRY_PREFIX, EXIT_PREFIX, build_rows
from .ledger import OPEN_STATUSES, compute_cash_state, flatten_orders
from .risk import Intent, RiskContext, check_entry, daily_limit_hit
from .skills import load_skill
from .state import StateStore
from .strategy import build_levels, find_signal, size_qty
from .timeutil import UTC, ET, hhmm, session_open, to_et

D = Decimal
PROFIT_GOAL = "daily profit goal reached"


def _status(o):
    return str(o.get("status", "")).lower().split(".")[-1]


class Runner:
    def __init__(self, cfg: Config, broker, data, audit: Audit, state: StateStore, kill_path, *,
                 submit=False, replay=False, now_fn=None, sleep_fn=_time.sleep):
        if submit and replay:
            raise SafetyError("replay mode can never submit orders")
        self.cfg, self.broker, self.data, self.audit, self.state = cfg, broker, data, audit, state
        self.kill_path, self.submit, self.replay = kill_path, submit, replay
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.sleep = sleep_fn
        self._assets = {}
        self._proposed = set()
        self._data_failures = 0
        self._alerted = set()
        self.restriction_problems = []
        self._tm = None  # trader-timemachine module, loaded on first use

    # ------------------------------------------------------------------ startup
    def startup(self):
        acct = self.broker.get_account()
        verify_paper_account(acct)  # raises SafetyError unless provably a paper account
        waived = self.cfg.accept_unrestricted_options_level
        problems, changed = ensure_restrictions(self.broker, apply=self.submit, waive_options=waived)
        self.restriction_problems = problems
        if waived:
            self.audit.log("options_level_check_waived", note="user-accepted: options level cannot be lowered on "
                           "this paper account and this runner has no options order path; other restrictions enforced")
        if changed:
            self.audit.log("restrictions_applied", changed=changed)
        if self.submit:
            sub = self.cfg.submission_problems()
            if sub:
                raise SafetyError("order submission stays disabled: " + "; ".join(sub))
            if problems:
                raise SafetyError("cannot verify long-only / cash-only restrictions: " + "; ".join(problems))
            if self._kill_requested():
                raise SafetyError("kill switch file exists; run `clear-kill` after you have reviewed why")
        self._reconcile(startup=True)
        self.audit.log("startup", mode="paper-run" if self.submit else "dry-run", replay=self.replay,
                       restriction_problems=problems, config_problems=self.cfg.problems())

    def _kill_requested(self):
        return self.kill_path.exists()

    def _alert_once(self, key, msg, **data):
        if key not in self._alerted:
            self._alerted.add(key)
            self.audit.alert(msg, **data)

    def _reconcile(self, startup=False):
        positions = self.broker.get_positions()
        plans = self.state.data["plans"]
        for p in positions:
            sym = p["symbol"]
            if sym not in plans and sym not in self.state.data["foreign"]:
                self.state.data["foreign"].append(sym)
                self.state.save()
                self.audit.alert("unexpected position (not opened by this runner): will not be touched",
                                 symbol=sym, qty=p.get("qty"))
        for o in self.broker.get_orders("open"):
            cid = str(o.get("client_order_id", ""))
            if not cid.startswith((ENTRY_PREFIX, EXIT_PREFIX)):
                self._alert_once("order-" + o["id"], "unexpected open order not created by this runner",
                                 symbol=o.get("symbol"), id=o["id"])
        return positions

    # ------------------------------------------------------------------ one iteration
    def tick(self, now):
        out = {"proposed": [], "submitted": [], "rejected": [], "skipped": []}
        if self._kill_requested():
            self.shutdown("kill switch")
            out["stopped"] = True
            return out
        if not self.replay:
            clock = self.broker.get_clock()
            if not clock["is_open"]:
                out["idle"] = "market closed"
                return out
        day = to_et(now).date()
        account = self.broker.get_account()
        verify_paper_account(account)
        positions = self._reconcile()
        open_orders = self.broker.get_orders("open")
        closed = self.broker.get_orders("closed", after=now - timedelta(days=8))
        orders = {o["id"]: o for o in open_orders + closed}
        trading_days = self.broker.get_trading_days(day - timedelta(days=10), day + timedelta(days=10))
        cash = compute_cash_state(account, positions, list(orders.values()), trading_days, day,
                                  self.cfg.capital_usd, self.cfg.execution.settlement_days,
                                  orders_truncated=len(closed) >= 500)
        day_pnl = D(str(account["equity"])) - D(str(account["last_equity"]))
        out.update(cash_available=cash.available, cash_ok=cash.ok, day_pnl=day_pnl)

        self._journal(closed, positions)
        self._prune(positions, open_orders, day)
        self._cancel_stale_entries(open_orders, now)
        self._check_protection(positions, open_orders, day)
        goal = self.cfg.daily_profit_target_usd
        if goal and day_pnl >= D(str(goal)) and str(day) not in self.state.data["halts"]:
            self.state.data["halts"][str(day)] = PROFIT_GOAL
            self.state.save()
            self.audit.alert("daily profit goal reached: no new entries today; closing this runner's positions",
                             day_pnl=str(day_pnl), goal=goal)
        self._time_exit(now, day, positions, open_orders, force=self.state.data["halts"].get(str(day)) == PROFIT_GOAL)

        if daily_limit_hit(self.cfg, day_pnl) and str(day) not in self.state.data["halts"]:
            self.state.data["halts"][str(day)] = "daily loss limit hit"
            self.state.save()
            self.audit.alert("daily loss limit hit: no new positions today", day_pnl=str(day_pnl))
        halted = self.state.data["halts"].get(str(day))
        if halted:
            out["halted"] = halted
            return out
        if to_et(now).time() > hhmm(self.cfg.strategy.entry_before):
            out["idle"] = "past entry window"
            return out
        self._entries(now, day, cash, positions, open_orders, day_pnl, out)
        return out

    # ------------------------------------------------------------------ entries
    def _entries(self, now, day, cash, positions, open_orders, day_pnl, out):
        st = self.cfg.strategy
        try:
            bars = self.data.get_bars(self.cfg.universe, session_open(day), now)
            self._data_failures = 0
        except Exception as e:  # noqa: BLE001 - any data failure must halt entries, never crash the loop
            self._data_failures += 1
            self.audit.log("data_error", error=repr(e), consecutive=self._data_failures)
            if self._data_failures >= self.cfg.data.failures_before_halt:
                self._alert_once("data-%s" % day, "market data failing: new entries halted until it recovers",
                                 error=repr(e))
            out["skipped"].append("market data error")
            return
        max_age = timedelta(seconds=self.cfg.data.max_bar_age_sec)
        signals = []
        fresh = 0
        for sym in self.cfg.universe:
            bs = bars.get(sym) or []
            if bs and now - (max(b.ts for b in bs) + timedelta(minutes=5)) <= max_age:
                fresh += 1
            sig = find_signal(sym, bs, day, st, known_until=now)
            if sig:
                signals.append(sig)
        if fresh == 0:
            self._alert_once("stale-%s-%s" % (day, now.strftime("%H")), "market data stale for the whole universe")
            out["skipped"].append("stale data")
            return
        quotes = {}
        if signals and not self.replay:
            try:
                quotes = self.data.get_latest_quotes([s.symbol for s in signals])
            except Exception as e:  # noqa: BLE001
                self.audit.log("quote_error", error=repr(e))
                out["skipped"].append("quote error")
                return
        halted = self.state.data["halts"].get(str(day))
        ctx = RiskContext(cfg=self.cfg, cash=cash, positions=positions, open_orders=list(open_orders),
                          day_pnl=day_pnl, open_risk=self._open_risk(positions), kill=self._kill_requested(),
                          halted=halted, data_ok=True, seen_ids=frozenset(self.state.data["sent_ids"]),
                          foreign_symbols=frozenset(self.state.data["foreign"]))
        for sig in sorted(signals, key=lambda s: (s.bar_end, s.symbol)):
            cid = "%s%s-%s-%s" % (ENTRY_PREFIX, day.strftime("%Y%m%d"), sig.symbol, to_et(sig.bar_end).strftime("%H%M"))
            if cid in self._proposed:
                continue
            if now - sig.bar_end > max_age:
                self._skip(out, sig.symbol, "signal bar is stale")
                continue
            ref = sig.ref_close
            if not self.replay:
                q = quotes.get(sig.symbol)
                why = self._bad_quote(q, now)
                if why:
                    self._skip(out, sig.symbol, why)
                    continue
                ref = max(q["ask"], sig.ref_close)
            lv = build_levels(ref, sig.stop, st)
            if lv is None:
                self._skip(out, sig.symbol, "stop distance / price outside strategy bounds")
                continue
            if not self._asset_ok(sig.symbol):
                self._skip(out, sig.symbol, "asset not an active tradable US equity")
                continue
            cfg = self.cfg
            room = min(cash.exposure_room(), D(str(cfg.max_account_exposure_usd)) - cash.open_cost - cash.reserved)
            qty = size_qty(lv, cfg.max_loss_per_trade_usd, cfg.max_order_notional_usd,
                           float(cash.available), float(max(D(0), room)))
            if qty < 1:
                self._skip(out, sig.symbol, "size rounds to 0 shares under the loss / cash / exposure caps")
                continue
            it = Intent(sig.symbol, qty, lv, cid)
            ctx.cash, ctx.open_orders = cash, ctx.open_orders
            violations = check_entry(it, ctx)
            ticket = {"symbol": it.symbol, "qty": qty, "limit": lv.limit, "stop": lv.stop, "target": lv.target,
                      "notional": str(it.notional), "max_loss_at_stop": str(it.max_loss), "client_order_id": cid,
                      "or_high": sig.or_high, "or_low": sig.or_low, "signal_bar_end": sig.bar_end.isoformat()}
            if violations:
                self.audit.log("entry_rejected_locally", ticket=ticket, violations=violations)
                out["rejected"].append((sig.symbol, violations))
                continue
            self._proposed.add(cid)  # each signal is simulated / sent at most once
            tm = self._time_machine(sig, lv, day, now)
            if tm is not None:
                ticket["time_machine"] = tm
                if not tm["pass"]:
                    self.audit.log("entry_skipped_time_machine", ticket=ticket)
                    self._skip(out, sig.symbol, "time machine: " + tm["why"])
                    continue
            if not self.submit:
                self.audit.log("PROPOSED_TICKET", ticket=ticket, note="dry-run: nothing was sent")
                out["proposed"].append(ticket)
            else:
                if not self._send(it, sig, day, ticket, out):
                    continue
            cash.reserve(it.notional)
            ctx.open_orders.append({"symbol": it.symbol, "side": "buy", "status": "new", "legs": []})

    def _time_machine(self, sig, lv, day, now):
        """trader-timemachine (the skill's own code, unchanged) on this exact plan, replayed to the flat-by time.
        Pass only if the replays' average R beats the estimated round-trip cost in R. Any error fails closed."""
        st, c = self.cfg.strategy, self.cfg.costs
        if not st.time_machine_gate:
            return None
        cost_r = lv.limit * 2 * (c.half_spread_bps + c.slippage_bps) / 1e4 / (lv.limit - lv.stop)
        try:
            self._tm = self._tm or load_skill("trader-timemachine", "timemachine")
            plan = self._tm.normalize_plan({
                "symbol": sig.symbol, "side": "buy", "date": str(day), "entry": lv.limit, "stop": lv.stop,
                "target": lv.target, "entry_time": to_et(sig.bar_end).strftime("%H:%M"), "setup": "orb",
                "stop_rule": "or_mid", "opening_range": {"minutes": st.or_minutes}})
            doc = self._tm.time_machine(self._tm.fetch_chart(sig.symbol), plan,
                                        now.astimezone(UTC).replace(tzinfo=None), until=st.flat_by)
        except Exception as e:  # noqa: BLE001 - no simulation, no trade
            return {"pass": False, "why": "unavailable (%s)" % e}
        mc, hist = doc["mc"], doc.get("history") or {}
        ok = mc["avg_R"] > cost_r
        return {"pass": ok, "why": "avg R %+.3f %s cost %.3fR" % (mc["avg_R"], ">" if ok else "<=", cost_r),
                "seed": mc.get("seed"), "replays": mc.get("replays"), "p_target": mc.get("p_target"),
                "p_stop": mc.get("p_stop"), "p_neither": mc.get("p_neither"), "avg_R": mc["avg_R"],
                "cost_R": round(cost_r, 3), "verdict": doc.get("verdict"),
                "history": {k: hist.get(k) for k in ("sessions", "triggered", "avg_R")}}

    def _skip(self, out, sym, why):
        self.audit.log("signal_skipped", symbol=sym, reason=why)
        out["skipped"].append((sym, why))

    def _bad_quote(self, q, now):
        if not q or not q.get("bid") or not q.get("ask") or q["ask"] < q["bid"]:
            return "no usable quote"
        if (now - q["ts"]).total_seconds() > self.cfg.data.max_quote_age_sec:
            return "quote is stale"
        mid = (q["ask"] + q["bid"]) / 2
        if (q["ask"] - q["bid"]) / mid * 1e4 > self.cfg.strategy.max_spread_bps:
            return "spread wider than max_spread_bps"
        return None

    def _asset_ok(self, sym):
        if sym not in self._assets:
            try:
                a = self.broker.get_asset(sym)
                self._assets[sym] = (a["asset_class"] == "us_equity" and a["status"] == "active"
                                     and a["tradable"] and str(a["exchange"]).upper() not in ("OTC", ""))
            except Exception as e:  # noqa: BLE001
                self.audit.log("asset_lookup_error", symbol=sym, error=repr(e))
                return False
        return self._assets[sym]

    def _send(self, it, sig, day, ticket, out):
        # id is persisted BEFORE the call: a crash mid-call can never double-send this signal
        self.state.mark_sent(it.client_order_id)
        try:
            o = self.broker.submit_bracket_buy(it.symbol, it.qty, it.levels.limit, it.levels.target,
                                               it.levels.stop, it.client_order_id)
        except Exception as e:  # noqa: BLE001 - a rejected order is logged, never retried
            self.audit.alert("order rejected / failed", ticket=ticket, error=repr(e))
            out["rejected"].append((it.symbol, [repr(e)]))
            return False
        self.state.data["plans"][it.symbol] = {"client_order_id": it.client_order_id, "day": str(day),
                                              "stop": it.levels.stop, "target": it.levels.target,
                                              "limit": it.levels.limit, "qty": it.qty}
        self.state.save()
        self.audit.log("order_submitted", ticket=ticket, broker_order_id=o.get("id"), status=o.get("status"))
        out["submitted"].append(ticket)
        return True

    def _open_risk(self, positions):
        total = D(0)
        for p in positions:
            plan = self.state.data["plans"].get(p["symbol"])
            if plan:
                total += max(D(0), D(str(p["avg_entry_price"])) - D(str(plan["stop"]))) * D(str(p["qty"]))
        return total

    # ------------------------------------------------------------------ order / position upkeep
    def _cancel_stale_entries(self, open_orders, now):
        if not self.submit:
            return
        for o in open_orders:
            if str(o.get("client_order_id", "")).startswith(ENTRY_PREFIX) and _status(o) in ("new", "accepted", "pending_new", "partially_filled"):
                age = (now - o["created_at"]).total_seconds() if o.get("created_at") else 0
                if age > self.cfg.execution.entry_timeout_sec:
                    try:
                        self.broker.cancel_order(o["id"])
                        self.audit.log("entry_canceled_timeout", symbol=o["symbol"], id=o["id"], age_sec=age)
                    except Exception as e:  # noqa: BLE001
                        self.audit.alert("cancel failed", id=o["id"], error=repr(e))

    def _check_protection(self, positions, open_orders, day):
        sells = {o["symbol"] for o in flatten_orders(open_orders) if str(o.get("side")).endswith("sell")}
        for p in positions:
            if p["symbol"] in self.state.data["plans"] and p["symbol"] not in sells:
                self._alert_once("unprotected-%s-%s" % (day, p["symbol"]),
                                 "position has no protective sell order at the broker (not auto-fixed)",
                                 symbol=p["symbol"], qty=p.get("qty"))

    def _time_exit(self, now, day, positions, open_orders, force=False):
        """Strategy rule: positions THIS RUNNER opened are sold at the flat-by time (or at once when `force`,
        i.e. the daily profit goal was reached). Foreign positions are never touched. Cancels the position's
        bracket legs first, then sells."""
        if not force and to_et(now).time() < hhmm(self.cfg.strategy.flat_by):
            return
        for p in positions:
            sym = p["symbol"]
            if sym not in self.state.data["plans"] or sym in self.state.data["foreign"]:
                continue
            qty = int(D(str(p["qty"])))
            if qty < 1:
                continue
            if any(str(o.get("client_order_id", "")).startswith(EXIT_PREFIX) and o["symbol"] == sym
                   for o in open_orders):
                continue  # an exit order is already working
            if not self.submit:
                self.audit.log("WOULD_EXIT_AT_FLAT_BY", symbol=sym, qty=qty, note="dry-run: nothing was sent")
                continue
            tries = self.state.data.setdefault("exit_tries", {})
            n = tries.get("%s-%s" % (day, sym), 0)
            if n >= 5:
                self._alert_once("exit-%s-%s" % (day, sym), "flat-by exit failed 5 times: manual action needed", symbol=sym)
                continue
            tries["%s-%s" % (day, sym)] = n + 1
            self.state.save()
            for leg in flatten_orders(open_orders):
                if leg["symbol"] == sym and str(leg.get("side")).endswith("sell") and _status(leg) in OPEN_STATUSES:
                    try:
                        self.broker.cancel_order(leg["id"])
                    except Exception as e:  # noqa: BLE001
                        self.audit.log("leg_cancel_error", symbol=sym, error=repr(e))
            try:
                o = self.broker.submit_market_sell(sym, qty, "%s%s-%s-%d" % (EXIT_PREFIX, day.strftime("%Y%m%d"), sym, n))
                self.audit.log("flat_by_exit_submitted", symbol=sym, qty=qty, broker_order_id=o.get("id"))
            except Exception as e:  # noqa: BLE001 - legs may still be cancelling; retried next tick (max 5)
                self.audit.log("flat_by_exit_retry", symbol=sym, error=repr(e), attempt=n + 1)

    def _journal(self, closed, positions):
        rows = build_rows(closed, positions, set(self.state.data["journaled"]), self.cfg.costs)
        for r in rows:
            self.audit.journal(r)
            self.state.data["journaled"].append(r["order_id"])
        if rows:
            self.state.save()
            for r in rows:
                self.state.data["plans"].pop(r["symbol"], None)
            self.state.save()

    def _prune(self, positions, open_orders, day):
        """Forget plans of entries that never filled (older days) and foreign flags of closed positions."""
        held = {p["symbol"] for p in positions}
        ordering = {o["symbol"] for o in flatten_orders(open_orders)}
        plans = self.state.data["plans"]
        stale = [s for s, p in plans.items() if s not in held and s not in ordering and p.get("day") != str(day)]
        gone = [s for s in self.state.data["foreign"] if s not in held]
        for s in stale:
            plans.pop(s)
        for s in gone:
            self.state.data["foreign"].remove(s)
        if stale or gone:
            self.state.save()

    # ------------------------------------------------------------------ shutdown / loop
    def shutdown(self, reason):
        """Cancel unfilled entries only. Never sells, never cancels protective legs."""
        canceled = []
        if self.submit:
            for o in self.broker.get_orders("open"):
                if str(o.get("client_order_id", "")).startswith(ENTRY_PREFIX) and (D(str(o.get("filled_qty") or 0)) == 0):
                    try:
                        self.broker.cancel_order(o["id"])
                        canceled.append(o["symbol"])
                    except Exception as e:  # noqa: BLE001
                        self.audit.alert("cancel failed during shutdown", id=o["id"], error=repr(e))
        positions = self.broker.get_positions()
        self.audit.alert("runner stopped: %s. Open positions are NOT liquidated; broker-side stops/targets remain."
                         % reason, canceled_unfilled_entries=canceled, open_positions=[p["symbol"] for p in positions])

    def _nap(self, r):
        return 30 if r.get("idle") == "market closed" else self.cfg.execution.poll_sec

    def run(self):
        self.startup()
        errors, beat = 0, None
        try:
            while True:
                try:
                    now = self.now_fn()
                    r = self.tick(now)
                    errors = 0
                    if beat is None or now - beat >= timedelta(minutes=15):  # proof of life in audit.jsonl
                        beat = now
                        self.audit.log("heartbeat", idle=r.get("idle"), halted=r.get("halted"),
                                       proposed=len(r.get("proposed", [])), submitted=len(r.get("submitted", [])),
                                       skipped=len(r.get("skipped", [])), cash_available=r.get("cash_available"))
                except SafetyError:
                    raise
                except Exception as e:  # noqa: BLE001 - transient API trouble: back off, alert, keep protecting
                    errors += 1
                    self.audit.alert("loop error", error=repr(e), consecutive=errors)
                    if errors >= 10:
                        raise
                    self.sleep(min(60, 5 * errors))
                    continue
                if r.get("stopped"):
                    return
                self.sleep(self._nap(r))
        except KeyboardInterrupt:
            self.shutdown("interrupted (Ctrl+C)")
        except BaseException as e:
            self.audit.alert("fatal error, runner exiting", error=repr(e))
            self.shutdown("fatal error")
            raise
