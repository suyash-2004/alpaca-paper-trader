"""Paper-only guards. Everything here fails closed: if paper-ness or a restriction
cannot be proven, SafetyError is raised and order submission stays off.

There is deliberately no live URL, no live credential variable and no mode switch
anywhere in this package. The trading endpoint is a constant."""
import os
import re
from decimal import Decimal, InvalidOperation

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"  # hard-coded, never configurable
ENV_KEY = "ALPACA_KEY"  # same names as the cloud environment
ENV_SECRET = "ALPACA_SECRET"
# Older names, still accepted so existing local setups keep working.
LEGACY_ENV_KEY = "ALPACA_PAPER_API_KEY"
LEGACY_ENV_SECRET = "ALPACA_PAPER_SECRET_KEY"
# Any of these set to something other than the paper URL means the environment is
# pointing at another endpoint; refuse rather than guess which one the SDK would use.
_URL_ENVS = ("APCA_API_BASE_URL", "ALPACA_API_BASE_URL", "ALPACA_TRADING_URL")
_PAPER_KEY_RE = re.compile(r"^PK[A-Z0-9]{8,}$")  # Alpaca paper key ids start with PK
_PAPER_ACCT_RE = re.compile(r"^PA[A-Z0-9]+$")  # Alpaca paper account numbers start with PA

# Restrictions that must hold before any order is sent: (config key, required value)
REQUIRED_CONFIG = (("no_shorting", True), ("max_margin_multiplier", "1"), ("max_options_trading_level", 0))


class SafetyError(RuntimeError):
    """A paper-only / cash-only requirement could not be proven."""


def normalize_url(url) -> str:
    """Lower-case, no trailing slash, and the dashboard's optional '/v2' API-version suffix dropped:
    the SDK appends /v2 itself, so .../v2 and the bare host are the same paper endpoint."""
    u = str(getattr(url, "value", url)).strip().rstrip("/").lower()
    return u[:-3] if u.endswith("/v2") else u


def assert_paper_endpoint(url) -> None:
    if normalize_url(url) != PAPER_TRADING_URL:
        raise SafetyError("trading endpoint is not the Alpaca paper endpoint: %r" % (str(url),))


def parse_dotenv(path) -> dict:
    """Minimal KEY=VALUE parser. Only paper credential names and the URL guard names are honoured."""
    out = {}
    if path and os.path.exists(path):
        for line in open(path, encoding="utf-8").read().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k in (ENV_KEY, ENV_SECRET, LEGACY_ENV_KEY, LEGACY_ENV_SECRET, *_URL_ENVS):
                out[k] = v
    return out


def load_credentials(env=None, dotenv_path=None):
    """Paper credentials from environment variables, falling back to a local .env file
    (paper_trader/.env, git-ignored; real environment variables win). Never printed or logged."""
    env = {**parse_dotenv(dotenv_path), **(os.environ if env is None else env)}
    for name in _URL_ENVS:
        val = (env.get(name) or "").strip()
        if val and normalize_url(val) != PAPER_TRADING_URL:
            raise SafetyError("environment variable %s points at a non-paper endpoint; unset it" % name)
    key = (env.get(ENV_KEY) or env.get(LEGACY_ENV_KEY) or "").strip()
    secret = (env.get(ENV_SECRET) or env.get(LEGACY_ENV_SECRET) or "").strip()
    if not key or not secret:
        raise SafetyError("set %s and %s (Alpaca PAPER keys) in your environment" % (ENV_KEY, ENV_SECRET))
    if not _PAPER_KEY_RE.match(key):
        raise SafetyError("%s does not look like a paper key id (expected prefix PK); refusing" % ENV_KEY)
    return key, secret


def verify_paper_account(acct: dict) -> None:
    """Prove from the API response that this is an active, unblocked paper USD account."""
    num = str(acct.get("account_number") or "")
    if not _PAPER_ACCT_RE.match(num):
        raise SafetyError("account number does not look like a paper account (expected prefix PA); refusing")
    if str(acct.get("status", "")).upper().split(".")[-1] != "ACTIVE":
        raise SafetyError("account status is %r, not ACTIVE" % acct.get("status"))
    if str(acct.get("currency", "")).upper() != "USD":
        raise SafetyError("account currency is not USD")
    if acct.get("trading_blocked") or acct.get("account_blocked"):
        raise SafetyError("account is blocked from trading")


def _dec(x):
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError):
        return None


OPTIONS_KEY = "max_options_trading_level"


def restriction_problems(cfg: dict, acct: dict, waive_options: bool = False) -> list:
    """Return the list of unmet/unverifiable restrictions (empty = verified).

    waive_options: the user's explicit, config-recorded waiver of ONLY the options-level requirement (Alpaca's
    paper accounts cannot go below level 3). Shorting, margin and suspend_trade stay enforced."""
    problems = []
    for key, want in REQUIRED_CONFIG:
        if waive_options and key == OPTIONS_KEY:
            continue
        have = cfg.get(key)
        if have is None:
            problems.append("%s: not reported by the API, cannot verify" % key)
        elif key == "max_margin_multiplier":
            if _dec(have) != _dec(want):
                problems.append("%s is %s, need %s" % (key, have, want))
        elif have != want:
            problems.append("%s is %r, need %r" % (key, have, want))
    if cfg.get("suspend_trade"):
        problems.append("suspend_trade is on: the account is blocking new orders")
    mult = acct.get("multiplier")
    if mult is None or _dec(mult) != Decimal(1):
        problems.append("account multiplier is %s, need 1 (no margin)" % mult)
    if acct.get("shorting_enabled") is not False:
        problems.append("account shorting_enabled is %r, need False" % acct.get("shorting_enabled"))
    lvl = acct.get("options_trading_level")
    if not waive_options and lvl not in (None, 0):
        problems.append("effective options level is %r, need 0" % lvl)
    return problems


def ensure_restrictions(broker, apply: bool, waive_options: bool = False):
    """Verify (and, if apply, set) long-only / 1x / options-off. Returns (problems, changed_keys).

    A non-empty problems list means order submission must stay disabled."""
    cfg = broker.get_configurations()
    changed = {}
    if apply:
        for key, want in REQUIRED_CONFIG:
            if waive_options and key == OPTIONS_KEY:
                continue
            have = cfg.get(key)
            same = _dec(have) == _dec(want) if key == "max_margin_multiplier" else have == want
            if not same:
                changed[key] = want
        if changed:
            broker.set_configurations(**changed)
            cfg = broker.get_configurations()
    return restriction_problems(cfg, broker.get_account(), waive_options), changed
