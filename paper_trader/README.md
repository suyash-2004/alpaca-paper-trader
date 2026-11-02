# paper_trader — Alpaca PAPER-only, long-only, cash-only automation

This is a separate runner next to the `day-trader-skill` pack (installed unchanged in `../.claude/skills/trader-*`).
The skill's per-order approval gates (`trader-risk` PASS → typed "yes" → Claude's Allow-once card) are untouched and
still work for manual use. This runner is a different program that, **only after you start `paper-run`**, sends
simulated orders to Alpaca's paper account by itself.

**Nothing here is evidence that any strategy is or will be profitable.** Backtests and paper results are simulations
(see *Limitations*). With nothing valid to trade, the runner stays idle.

## Hard rules enforced in code (and tested)

| rule | where |
| --- | --- |
| Trading endpoint is the constant `https://paper-api.alpaca.markets`; not configurable; no live URL, live credential variable or live switch exists. Unknown config keys (`endpoint`, `live`, `margin`, …) are rejected. | `guards.py`, `config.py` |
| Credentials only from `ALPACA_KEY` / `ALPACA_SECRET` (the older `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY` still work): real env vars, else the git-ignored `paper_trader/.env` (your choice; plain text on disk). Never printed or logged (redacted incl. substring match), never committed. | `guards.py`, `audit.py` |
| Fail closed at startup: key id must start `PK`; any `APCA_API_BASE_URL`-style env var pointing elsewhere refuses; account number must look like a paper account (`PA…`), status `ACTIVE`, USD, not blocked; SDK client base URL is asserted equal to the paper URL. | `guards.py`, `alpaca_adapter.py` |
| Long-only US stocks, no options/crypto/margin/leverage. `paper-run` **sets** `no_shorting=true`, `max_margin_multiplier="1"`, `max_options_trading_level=0` on the paper account and **re-reads to verify** (also account `multiplier==1`, `shorting_enabled==False`). If anything can't be verified, it refuses to start. `dry-run`/`verify` only read. | `guards.ensure_restrictions` |
| Sizing uses **only** a settled-cash ledger: `min(cash − fees − pending transfers out − unsettled sale proceeds − cash reserved by open buy orders, capital_usd − open cost − reserved)`. Alpaca `buying_power`, `regt_buying_power`, `daytrading_buying_power` are never read. T+1 settlement from Alpaca's trading calendar. Unknown cash / calendar / truncated order history → no buys. | `ledger.py` |
| Entries are marketable **limit** orders, so worst-case cost is `qty × limit`. That notional (plus everything already reserved) must be ≤ available settled cash or the order is rejected **locally before any broker call**. | `risk.check_entry` |
| Hard limits (yours, no defaults): per-trade loss, daily loss, max open positions, max order notional, max account exposure. After the daily limit is hit, no new positions that day (latched). Worst-case check also rejects entries that could breach the daily limit via open risk + this stop. | `risk.py`, `runner.py` |
| Duplicate protection: deterministic `client_order_id` (`ptr-e-YYYYMMDD-SYM-HHMM`), persisted **before** the call (survives crash/restart); one position/open order per symbol; rejected orders are never retried. | `state.py`, `runner._send` |
| Stale data: newest completed bar ≤ `max_bar_age_sec`, signal bar must be the newest bar (no chasing), quote ≤ `max_quote_age_sec`, spread ≤ `max_spread_bps`. Repeated data failures halt entries and alert. | `runner.py` |
| Order submission stays off until `order_submission_enabled: true` **and** every required value is set **and** you run `paper-run`. | `config.py` |

## Strategy (deterministic, no LLM in the loop)

Opening-range breakout, long only, adapted from the skill (`trader-scanner orb`, `trader-exit or-mid / 1R`):
opening range = first 15 min; signal = first completed 5-min bar closing above the range high (only while it is the
newest bar, ≤ `entry_before` 15:30 ET, not >1 % extended); stop = range midpoint; target = `target_r` × risk; bracket
(stop + target held at Alpaca) with DAY time-in-force. Shares = smallest of the loss, order-size, settled-cash and
exposure caps (whole shares).

### Review of the skill: what I reused and the mismatches

Reviewed the SKILL.md of scanner, news, exit, timemachine, budget, risk and broker, and grep-scanned every script for
network/exec calls (only read-only HTTPS to Yahoo, Nasdaq Trader, SEC, House Clerk; one optional `pdftotext` call in
the Pelosi tool).

* **Reused (re-implemented, not imported):** ORB definition and "every bar present" usability rule (scanner), midpoint
  stop / R-multiple target / 15:55 flat-by (exit), loss-at-stop sizing + entry window + trades limits idea (budget/risk).
* **Scanner:** runs on Yahoo's unofficial, possibly delayed chart endpoint. The runner uses Alpaca IEX bars (official, but
  IEX is only a slice of consolidated volume, so prints/highs/lows differ and thin names may have gaps → skipped).
  Universe is yours (`universe`), not 5,900 symbols.
* **News:** headline RSS + SEC feed; the skill itself says headlines are leads to be *read*, not acted on. That is not
  deterministic, has no historical archive (cannot be backtested without look-ahead), so it is **not** used in automated
  entries. Mitigation: keep earnings-heavy names out of `universe`, and use `/trader-news` manually.
* **Time Machine:** bootstraps one stock's last 60 days; useful as a sanity check, but in-sample, one symbol, i.i.d. bars.
  Not used for validation; replaced by dev / walk-forward / holdout over your date ranges.
* **Budget/Risk:** size from a `budget` config and a local portfolio file, ignore settlement, and count daily loss as
  *max loss at stops of entries placed*. The runner uses the broker's actual cash/settlement and equity − last_equity.
* **Broker:** by design requires a typed yes per order. Unchanged; not used by the runner.
* Default target in the skill's exit is 2R (README example shows 1R). The grid below covers 1R / 1.5R / 2R.

## Setup (Windows / PowerShell, from this folder)

```powershell
cd "C:\Users\SuyashSinghBhadouria\Desktop\trading tool\paper_trader"
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # already done once; versions are pinned
copy config.template.json config.json                           # config.json is git-ignored
```

Credentials: create **paper** API keys in the Alpaca dashboard (paper view), then either
`copy .env.example .env` and fill in the two values in your editor (`.env` is git-ignored; plain text on disk), or set
`ALPACA_KEY` / `ALPACA_SECRET` (the older `ALPACA_PAPER_API_KEY` / `ALPACA_PAPER_SECRET_KEY` still work) as Windows user environment variables (env vars win over `.env`).
Never paste them into chat. A `.env` containing a non-paper URL variable or a non-`PK` key makes the runner refuse.

### Values you must provide in `config.json` (all start as `null`; nothing is assumed)

`capital_usd` (principal the runner may ever use), `max_loss_per_trade_usd`, `max_daily_loss_usd`, `max_open_positions`,
`max_order_notional_usd`, `max_account_exposure_usd`, `universe` (list of plain tickers, e.g. liquid large caps; 1–5
capital letters), and finally `order_submission_enabled: true` when you want `paper-run` to send orders. For `backtest`
also `evaluation.data_start / dev_end / walk_forward_end / holdout_end`. Cost/strategy/data settings have documented
defaults you can change. Notional and exposure limits above `capital_usd` are rejected (cash-only).

## Commands

```powershell
# 1. read-only checks (credentials, paper proof, restriction status, clock, IEX data). Sends no orders, changes nothing.
.\.venv\Scripts\python.exe -m papertrader verify

# 2. dry-run: proposes tickets, never sends orders
.\.venv\Scripts\python.exe -m papertrader dry-run --once
.\.venv\Scripts\python.exe -m papertrader dry-run                      # loops until Ctrl+C (market hours)
.\.venv\Scripts\python.exe -m papertrader dry-run --replay "2026-10-27 10:30"   # past session moment, ET

# 3. START automated PAPER trading (must stay running during US market hours, 9:30–16:00 ET)
.\.venv\Scripts\python.exe -m papertrader paper-run

# 4. STOP: Ctrl+C in that window, or from another terminal:
.\.venv\Scripts\python.exe -m papertrader stop          # then, before restarting:  ... clear-kill

# evaluation
.\.venv\Scripts\python.exe -m papertrader backtest      # dev -> walk-forward -> one-shot holdout; writes reports/
.\.venv\Scripts\python.exe -m papertrader report        # paper-forward results from state/trades.jsonl
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .    # tests (mocked API, no network)
```

No service, scheduled task, startup entry or cloud deployment is configured. When the process isn't running, nothing
trades. Logs/state (git-ignored): `state/audit.jsonl`, `state/alerts.log` (errors, unexpected positions/orders,
unprotected positions), `state/trades.jsonl`, `state/state.json`, `state/KILL`.

## Exit and shutdown behaviour (no blind liquidation)

* **Normal trade end:** Alpaca holds the stop and target legs (bracket) — they work even if this program is not running.
* **Scheduled exit (strategy rule):** at/after `flat_by` (15:55 ET) positions **this runner opened** (recorded in
  `state.json`) get their legs cancelled and a market sell for the held quantity. Positions it did not open are never sold.
  Brackets are DAY orders, so this rule avoids holding unprotected positions overnight.
* **Kill switch / Ctrl+C / fatal error:** stop opening positions, cancel only *unfilled* entry orders, alert. Open positions
  and their stop/target legs are left in place. Nothing is liquidated. `paper-run` refuses to start while `state/KILL` exists.
* **Process crash / reboot:** legs stay at Alpaca for the day. If you restart later, positions not recorded in `state.json`
  are reported as unexpected and never touched; reconcile by hand. A held position with no protective sell order raises an
  ALERT (not auto-fixed).
* **Market-data failure:** new entries halt (alert after `failures_before_halt` consecutive failures) and resume when data
  returns; broker-side legs and the flat-by exit don't need market data.
* **Alpaca/API errors:** loop backs off; 10 consecutive errors → fatal path above. A rejected order is logged, alerted, not retried.
* **Daily loss limit:** no new positions for the rest of the day; existing positions keep their legs.

## Evaluation method (what `backtest` does)

Same signal/levels/sizing code as live. Entry on the *next* bar's open after the signal bar closes (no look-ahead);
half-spread + slippage on entries and exits, extra slippage on stops, SEC/TAF fees on sells (conservative defaults — check
current rate schedules), 10 % random partial fills (Alpaca's paper behaviour), random rejections, stop-first when a bar
touches both levels, T+1 settled-cash rule, all risk limits. Three separate periods:

1. **Development** — the only place parameters are picked (small fixed grid, ≥ `min_trades_to_select` trades).
2. **Walk-forward** — folds of `wf_test_days`; parameters re-picked for each fold using only earlier days.
3. **Holdout** — run once with parameters chosen from earlier data; a lock file refuses any later run with different parameters.
4. **Paper-forward** — `report` reads the runner's own journal (real paper fills, estimated fees).

Reports include benchmark (SPY buy-and-hold), trades, gross/net P&L, estimated costs, max drawdown, win/loss distribution
(percentiles, best/worst, exit reasons), average capital exposure, skip/reject diagnostics and all assumptions.

### Research status (2026-10-29): nothing passes in a deployable form

| Family | Script | Result |
|---|---|---|
| ORB (skill default) + V0–V5 variants | `backtest`, `research` | negative net in every period |
| Skill daily setups gating ORB (K0–K3, SIP) | `research_intraday` | negative net |
| Intraday momentum (M0–M2, index ETFs 2016–2023) | `research_momentum` | negative net and ~zero gross; holdout left unopened |
| Overnight holds (B0, D1–D3, S1; SIP split-adjusted daily) | `research_overnight` | D2 RSI(2) pullback passed dev/val/holdout as tested, but the deployable form (one order ≤ `max_order_notional_usd` + 7 % GTC stop so the per-trade loss limit holds overnight) lost in 2017–2020 (`--deployed`) |

Because no active rule beat holding the index, `config.json` now has `"hold": {"enabled": true}` (see below).
The intraday ORB loop is still in the code and runs when `hold.enabled` is false.

### Recorded waiver: options level (2026-10-29)

Requirement "options level 0" **cannot be met** on this paper account: Alpaca rejects it from the API (`max_options_trading_level
provided 0 is less than minimum allowed value 3`) and from the dashboard, the configuration endpoint does not report the field,
and Alpaca states that all paper accounts have Level 3. The user answered "no options trading needed" after being told what
waiving means, so `config.json` sets `accept_unrestricted_options_level: true`. It waives **only** the options-level check
(`guards.restriction_problems(waive_options=True)`); no-short, 1x margin, `suspend_trade` and the paper proofs are still
enforced and applied at startup, and every startup logs `options_level_check_waived`. Why this is low risk: the adapter has
no options/short/bulk call (`tests/test_guards.py::NoOptionsPath` pins its public surface), tickers must be 1–5 plain letters
so an option-contract symbol is rejected before any order, and assets must be active `us_equity`. Set the flag back to `false`
to restore the strict check (the runner then refuses to start on this account).

### Hold mode (`papertrader/hold.py`)

Buys `hold.symbol` (SPY) with a plain DAY limit order (ask + `limit_slippage_bps`, whole shares, settled cash only) between
`entry_after` and `entry_before` until `max_account_exposure_usd` is invested, then does nothing. This earns the market
return, not an edge: `python -m papertrader.research_overnight --hold` shows the seen-data path (2017–2026: +$19.1k on
$8k invested, worst drawdown 29 %, worst day –$1,100); it is not out-of-sample evidence. Paper accounts pay no dividends,
so paper returns understate a real SPY holder's.

What changed from the intraday rules, on purpose: **no stop and no automatic sale.** `max_loss_per_trade_usd` and
`max_daily_loss_usd` do not apply (there is no stop distance to size from). The loss bound is `max_account_exposure_usd`;
an unrealized loss from the high-water mark of `hold.alert_drawdown_pct` of `capital_usd` (20 % = $2,000) raises one ALERT
(`state/alerts.log`) and you decide. Everything else is shared: paper proof, restrictions, kill switch (`stop`), settled-cash
ledger, duplicate ids, stale-quote check, reconciliation (a SPY position this runner did not open is never touched), and
`shutdown` cancels unfilled orders but never sells. Selling is manual (Alpaca dashboard).

## Known limitations of paper trading and of this system

* Alpaca paper fills at the NBBO with random 10 % partial fills; it ignores market impact, queue position, order size vs.
  displayed liquidity, price improvement, latency slippage, regulatory fees and dividends. Live results can differ a lot.
* Free paper accounts get IEX-only data: thin symbols have missing/stale bars and quotes; signals can differ from consolidated data.
* 5-minute bars hide intrabar order of events; the backtest assumes the worst case for stops, which is still an assumption.
* Parameter selection on historical data can overfit even with a holdout; one holdout is one sample.
* Settlement T+1 is modelled from the trading calendar; the broker's own paper rules may differ. Unknown → no buys.
* The "paper" proof relies on Alpaca conventions (`PK` key ids, `PA` account numbers) plus the hard-coded paper endpoint.
  If Alpaca changes those conventions the runner refuses to start (fail closed) rather than guessing.
* After `paper-run` changes `max_margin_multiplier`, Alpaca may apply the new `multiplier` on its own schedule; the runner
  refuses to start until the account itself reports 1x.
* Gaps can fill stops far below the stop price; per-trade loss limits are measured at the stop.
* Not financial advice.
