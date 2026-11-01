"""Configuration. Risk limits and capital have NO defaults: they must come from the user."""
import json
from dataclasses import dataclass, field, fields
from pathlib import Path

REQUIRED_USER_FIELDS = (
    "capital_usd", "max_loss_per_trade_usd", "max_daily_loss_usd",
    "max_open_positions", "max_order_notional_usd", "max_account_exposure_usd", "universe",
)


@dataclass
class StrategyParams:
    or_minutes: int = 15            # opening range length (skill default)
    target_r: float = 1.0           # take-profit at this multiple of risk (skill default 1R)
    min_price: float = 5.0          # skill scanner default price floor
    entry_before: str = "15:30"     # no new entries after this ET time (skill default)
    flat_by: str = "15:55"          # scheduled exit of runner-owned positions (skill default)
    max_extension_pct: float = 1.0  # skip a breakout bar that closed >1% above the range (chasing)
    min_risk_pct: float = 0.2       # stop must be at least this far below entry (% of price)
    max_risk_pct: float = 3.0       # ...and at most this far
    limit_slippage_bps: float = 10  # entry is a marketable LIMIT this far above the ask: caps cost
    max_spread_bps: float = 15      # live only: skip when the quoted spread is wider
    min_rel_volume: float = 0.0     # 0 = off. Opening-range volume vs prior-14-session mean ("stock in play")
    stop_mode: str = "or_mid"       # "or_mid" (range midpoint) or "atr" (entry - atr_stop_frac x 14-day ATR)
    atr_stop_frac: float = 0.10
    market_filter: bool = False     # only enter while SPY is above its own session open
    time_machine_gate: bool = False  # live only: trader-timemachine must show avg R above round-trip cost (fails closed)
    max_entries: int = 1            # breakouts per symbol per day; >1 re-enters after a close back under the range high


@dataclass
class Costs:
    half_spread_bps: float = 3.0
    slippage_bps: float = 2.0
    stop_slippage_bps: float = 10.0  # stops are market orders, fill worse
    sec_fee_per_million: float = 27.80   # conservative; sells only. Check current SEC rate.
    taf_per_share: float = 0.000166      # FINRA TAF, sells only. Check current rate.
    taf_max: float = 8.30
    partial_fill_prob: float = 0.10      # Alpaca paper docs: 10% of fills are partial
    partial_fill_min: float = 0.10
    partial_fill_max: float = 0.90
    reject_prob: float = 0.01


@dataclass
class DataCfg:
    max_bar_age_sec: int = 150      # newest completed 5-min bar may be at most this old
    max_quote_age_sec: int = 60
    failures_before_halt: int = 3   # consecutive data failures before new entries halt


@dataclass
class ExecCfg:
    poll_sec: int = 10
    entry_timeout_sec: int = 120    # unfilled entry orders are canceled after this
    settlement_days: int = 1        # US equities settle T+1


@dataclass
class HoldCfg:
    """Buy-and-hold mode (replaces the intraday ORB loop when enabled). There is NO stop and NO automatic sale:
    the loss bound is max_account_exposure_usd, and a drawdown only raises an alert."""
    enabled: bool = False
    symbol: str = "SPY"
    alert_drawdown_pct: float = 20.0   # alert when unrealized loss from its high-water mark >= this % of capital_usd
    entry_after: str = "09:45"         # ET; skip the opening auction noise
    entry_before: str = "15:30"
    max_attempts_per_day: int = 3      # unfilled limit orders are canceled after execution.entry_timeout_sec and retried


@dataclass
class EvalCfg:
    data_start: str = None
    dev_end: str = None
    walk_forward_end: str = None
    holdout_end: str = None
    wf_test_days: int = 20
    min_trades_to_select: int = 30
    seed: int = 7
    param_grid: dict = field(default_factory=lambda: {"or_minutes": [15, 30], "target_r": [1.0, 1.5, 2.0]})


_SECTIONS = {"strategy": StrategyParams, "costs": Costs, "data": DataCfg, "execution": ExecCfg, "hold": HoldCfg,
             "evaluation": EvalCfg}
_TOP = {"order_submission_enabled", "accept_unrestricted_options_level", "daily_profit_target_usd",
        *REQUIRED_USER_FIELDS, *_SECTIONS}
_FORBIDDEN_HINT = ("endpoint", "url", "live", "paper", "margin", "short", "leverage", "option", "crypto", "key", "secret")


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    order_submission_enabled: bool = False
    # The user's explicit waiver of ONLY the options-level-0 requirement. Alpaca refuses to lower this paper
    # account's options level below 3 (API and dashboard), and this system has no code path that can place an
    # options order. No-short and 1x-margin stay enforced. Logged at every startup.
    accept_unrestricted_options_level: bool = False
    # Optional. When the day's P&L reaches this, no new entries for the day and the runner's own positions are
    # closed (same path as the flat-by exit). None = off. A goal, not a forecast.
    daily_profit_target_usd: float = None
    capital_usd: float = None
    max_loss_per_trade_usd: float = None
    max_daily_loss_usd: float = None
    max_open_positions: int = None
    max_order_notional_usd: float = None
    max_account_exposure_usd: float = None
    universe: list = None
    strategy: StrategyParams = field(default_factory=StrategyParams)
    costs: Costs = field(default_factory=Costs)
    data: DataCfg = field(default_factory=DataCfg)
    execution: ExecCfg = field(default_factory=ExecCfg)
    hold: HoldCfg = field(default_factory=HoldCfg)
    evaluation: EvalCfg = field(default_factory=EvalCfg)

    def missing(self):
        out = []
        for name in REQUIRED_USER_FIELDS:
            v = getattr(self, name)
            if v is None or (name == "universe" and not v):
                out.append(name)
        return out

    def problems(self):
        """Missing values plus sanity violations. Empty list = usable for sizing."""
        probs = ["%s is not set" % m for m in self.missing()]
        if probs:
            return probs
        for name in REQUIRED_USER_FIELDS[:-1]:
            if getattr(self, name) <= 0:
                probs.append("%s must be > 0" % name)
        if self.max_order_notional_usd > self.capital_usd:
            probs.append("max_order_notional_usd exceeds capital_usd (cash-only: cannot exceed principal)")
        if self.max_account_exposure_usd > self.capital_usd:
            probs.append("max_account_exposure_usd exceeds capital_usd (cash-only: cannot exceed principal)")
        if self.max_loss_per_trade_usd > self.max_daily_loss_usd:
            probs.append("max_loss_per_trade_usd exceeds max_daily_loss_usd")
        if int(self.max_open_positions) != self.max_open_positions:
            probs.append("max_open_positions must be a whole number")
        bad = [s for s in self.universe if not (isinstance(s, str) and s.isalpha() and s.isupper() and 1 <= len(s) <= 5)]
        if bad:
            probs.append("universe has non plain-ticker symbols (A-Z, 1-5 letters only): %s" % bad[:5])
        if self.daily_profit_target_usd is not None and not self.daily_profit_target_usd > 0:
            probs.append("daily_profit_target_usd must be > 0 (or null for off)")
        h = self.hold
        if h.enabled:
            if not (isinstance(h.symbol, str) and h.symbol.isalpha() and h.symbol.isupper() and 1 <= len(h.symbol) <= 5):
                probs.append("hold.symbol must be a plain US ticker (A-Z, 1-5 letters)")
            if not 0 < h.alert_drawdown_pct <= 100:
                probs.append("hold.alert_drawdown_pct must be in (0, 100]")
            if int(h.max_attempts_per_day) != h.max_attempts_per_day or h.max_attempts_per_day < 1:
                probs.append("hold.max_attempts_per_day must be a whole number >= 1")
        return probs

    def submission_problems(self):
        probs = self.problems()
        if not self.order_submission_enabled:
            probs.append("order_submission_enabled is false")
        return probs


def _build(cls, raw, where):
    names = {f.name for f in fields(cls)}
    unknown = set(raw) - names
    if unknown:
        raise ConfigError("unknown key(s) in %s: %s" % (where, sorted(unknown)))
    return cls(**raw)


def load_config(path) -> Config:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(raw) - _TOP
    if unknown:
        hint = " (no endpoint/live/margin/short/options switches exist by design)" if any(
            h in k.lower() for k in unknown for h in _FORBIDDEN_HINT) else ""
        raise ConfigError("unknown top-level key(s): %s%s" % (sorted(unknown), hint))
    kw = {k: v for k, v in raw.items() if k not in _SECTIONS}
    for name, cls in _SECTIONS.items():
        kw[name] = _build(cls, raw.get(name, {}), name)
    for flag in ("order_submission_enabled", "accept_unrestricted_options_level"):
        if not isinstance(kw.get(flag, False), bool):
            raise ConfigError("%s must be true or false" % flag)
    return Config(**kw)
