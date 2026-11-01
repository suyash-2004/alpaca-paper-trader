"""Buy-and-hold mode: accumulate one US index ETF (default SPY) with settled cash, then hold.

Not a profit claim: this earns market return (minus drawdowns). Differences from the intraday runner:
- plain DAY limit BUY, no bracket: there is NO stop and NO automatic sale. The loss bound is
  max_account_exposure_usd; a drawdown past hold.alert_drawdown_pct of capital only raises an ALERT.
- max_loss_per_trade_usd / max_daily_loss_usd do not apply (a hold has no per-trade stop to size from).
- everything else is shared with Runner: paper proof, restrictions, kill switch, settled-cash ledger,
  duplicate ids, stale-quote check, reconciliation (positions not opened here are never touched).
- shutdown() still cancels unfilled entries only and never sells."""
from datetime import timedelta
from decimal import Decimal

from .guards import verify_paper_account
from .journal import ENTRY_PREFIX
from .ledger import compute_cash_state
from .runner import Runner
from .timeutil import hhmm, to_et

D = Decimal


class HoldRunner(Runner):
    def _nap(self, r):
        return 120 if r.get("idle") == "market closed" else 60 if r.get("idle") else self.cfg.execution.poll_sec

    def tick(self, now):
        out = {"proposed": [], "submitted": [], "rejected": [], "skipped": []}
        if self._kill_requested():
            self.shutdown("kill switch")
            out["stopped"] = True
            return out
        if not self.replay and not self.broker.get_clock()["is_open"]:
            out["idle"] = "market closed"
            return out
        cfg, h = self.cfg, self.cfg.hold
        day = to_et(now).date()
        account = self.broker.get_account()
        verify_paper_account(account)
        positions = self._reconcile()
        open_orders = self.broker.get_orders("open")
        closed = self.broker.get_orders("closed", after=now - timedelta(days=8))
        trading_days = self.broker.get_trading_days(day - timedelta(days=10), day + timedelta(days=10))
        cash = compute_cash_state(account, positions, open_orders + closed, trading_days, day, cfg.capital_usd,
                                  cfg.execution.settlement_days, orders_truncated=len(closed) >= 500)
        out.update(cash_available=cash.available, cash_ok=cash.ok)
        self._prune(positions, open_orders, day)
        self._cancel_stale_entries(open_orders, now)
        self._watch_drawdown(positions, day)
        if not hhmm(h.entry_after) <= to_et(now).time() <= hhmm(h.entry_before):
            out["idle"] = "outside entry window"
            return out
        self._buy(now, day, cash, open_orders, out)
        return out

    def _watch_drawdown(self, positions, day):
        """Unrealized P&L of the runner-owned position vs its high-water mark. Alert only; never sells."""
        sym, st = self.cfg.hold.symbol, self.state.data.setdefault("hold", {})
        before = dict(st)
        owned = sym in self.state.data["plans"] and sym not in self.state.data["foreign"]
        held = [p for p in positions if p["symbol"] == sym] if owned else []
        pnl = sum((D(str(p["market_value"])) - D(str(p["cost_basis"])) for p in held), D(0))
        hwm = max(D(st.get("hwm_pnl", "0")), pnl)
        st["hwm_pnl"] = str(hwm)
        limit = D(str(self.cfg.capital_usd)) * D(str(self.cfg.hold.alert_drawdown_pct)) / 100
        if hwm - pnl >= limit and not st.get("alerted"):
            st["alerted"] = True
            self.audit.alert("drawdown alert: unrealized loss from high-water mark reached the limit. "
                             "Nothing is sold automatically; decide whether to hold.", symbol=sym,
                             unrealized_pnl=str(pnl), high_water_mark=str(hwm), limit=str(limit))
        elif pnl >= hwm:
            st["alerted"] = False
        if st.get("status_day") != str(day):
            st["status_day"] = str(day)
            self.audit.log("hold_status", symbol=sym, day=str(day), unrealized_pnl=str(pnl), high_water_mark=str(hwm),
                           qty=[p["qty"] for p in held])
        if st != before:
            self.state.save()

    def _buy(self, now, day, cash, open_orders, out):
        cfg, h = self.cfg, self.cfg.hold
        sym = h.symbol
        if sym in self.state.data["foreign"]:
            out["skipped"].append((sym, "held outside this runner: not touched"))
            return
        room = min(cash.exposure_room(), D(str(cfg.max_account_exposure_usd)) - cash.open_cost - cash.reserved)
        want = min(max(D(0), room), cash.available, D(str(cfg.max_order_notional_usd)))
        if want < 1:
            out["idle"] = "target reached"
            return
        try:
            q = self.data.get_latest_quotes([sym]).get(sym) if not self.replay else None
        except Exception as e:  # noqa: BLE001 - quote failure just skips this tick
            self.audit.log("quote_error", error=repr(e))
            out["skipped"].append("quote error")
            return
        why = self._bad_quote(q, now) if q else "no usable quote"
        if why:
            out["skipped"].append((sym, why))
            return
        limit = round(q["ask"] * (1 + cfg.strategy.limit_slippage_bps / 1e4), 2)
        qty = int(want / D(str(limit)))
        if qty < 1:
            out["idle"] = "target reached"
            return
        if any(str(o.get("client_order_id", "")).startswith(ENTRY_PREFIX) and o["symbol"] == sym for o in open_orders):
            out["idle"] = "entry order working"
            return
        if not self._asset_ok(sym):
            out["skipped"].append((sym, "asset not an active tradable US equity"))
            return
        tries = self.state.data.setdefault("hold", {}).setdefault("tries", {})
        n = tries.get(str(day), 0)
        if n >= h.max_attempts_per_day:
            self._alert_once("tries-%s" % day, "hold buy attempted %d times today without filling: waiting for tomorrow" % n)
            out["idle"] = "max attempts today"
            return
        notional = D(str(limit)) * qty
        problems = self._problems(notional, cash)
        cid = "%s%s-%s-H%d" % (ENTRY_PREFIX, day.strftime("%Y%m%d"), sym, n + 1)
        if cid in self.state.data["sent_ids"]:
            problems.append("duplicate client_order_id")
        ticket = {"symbol": sym, "qty": qty, "limit": limit, "notional": str(notional), "client_order_id": cid}
        if problems:
            self.audit.log("entry_rejected_locally", ticket=ticket, violations=problems)
            out["rejected"].append((sym, problems))
            return
        if not self.submit:
            if cid not in self._proposed:
                self._proposed.add(cid)
                self.audit.log("PROPOSED_TICKET", ticket=ticket, note="dry-run: nothing was sent")
                out["proposed"].append(ticket)
            return
        self.state.mark_sent(cid)  # persisted BEFORE the call: a crash mid-call can never double-send
        tries[str(day)] = n + 1
        try:
            o = self.broker.submit_limit_buy(sym, qty, limit, cid)
        except Exception as e:  # noqa: BLE001 - a rejected order is logged, never retried this tick
            self.audit.alert("order rejected / failed", ticket=ticket, error=repr(e))
            out["rejected"].append((sym, [repr(e)]))
            self.state.save()
            return
        self.state.data["plans"][sym] = {"client_order_id": cid, "day": str(day), "qty": qty, "limit": limit}
        self.state.save()
        self.audit.log("order_submitted", ticket=ticket, broker_order_id=o.get("id"), status=o.get("status"))
        out["submitted"].append(ticket)

    def _problems(self, notional, cash):
        cfg, v = self.cfg, []
        if cfg.problems():
            v.append("config incomplete: " + "; ".join(cfg.problems()))
        if self._kill_requested():
            v.append("kill switch is on")
        if notional > D(str(cfg.max_order_notional_usd)):
            v.append("order notional $%s exceeds max_order_notional_usd" % notional)
        if not cash.ok:
            v.append("settled cash unknown: " + "; ".join(cash.reasons))
        elif notional > cash.available:
            v.append("order notional $%s exceeds available settled cash $%s" % (notional, cash.available))
        if cash.open_cost + cash.reserved + notional > D(str(cfg.max_account_exposure_usd)):
            v.append("would exceed max_account_exposure_usd")
        return v
