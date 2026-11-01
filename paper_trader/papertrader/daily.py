"""Daily-bar, long-only, cash-only portfolio simulator and three PRE-REGISTERED textbook strategies.

No parameter is tuned here. The numbers come from the original publications (Faber 2007 200-day
trend; Connors RSI(2); 12-1 month momentum) and were fixed before any result was seen, so the
2016+ history is genuinely out-of-sample for them.

Execution model: signal uses data through the close of day t-1; the trade happens at day t's OPEN
(no look-ahead). Whole shares only; buys can use SETTLED cash only (sale proceeds are usable the
next trading day, T+1); never more than capital; costs on both sides. Prices are raw (unadjusted):
Alpaca paper does not pay dividends either, so this matches what a paper account will show."""
from statistics import mean, pstdev

from .journal import sell_fees


def sma(xs, n):
    return [None if i + 1 < n else mean(xs[i + 1 - n:i + 1]) for i in range(len(xs))]


def rsi_wilder(xs, n):
    out, gain, loss = [None] * len(xs), 0.0, 0.0
    for i in range(1, len(xs)):
        ch = xs[i] - xs[i - 1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            gain, loss = gain + g / n, loss + l / n
        else:
            gain, loss = (gain * (n - 1) + g) / n, (loss * (n - 1) + l) / n
        if i >= n:
            out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out


class Data:
    """bars: {sym: [(date, o, h, l, c, v)]} -> aligned on the SPY calendar."""

    def __init__(self, bars):
        self.dates = [b[0] for b in bars["SPY"]]
        self.open, self.low, self.close = {}, {}, {}
        for s, rows in bars.items():
            m = {r[0]: r for r in rows}
            self.open[s] = [m[d][1] if d in m else None for d in self.dates]
            self.low[s] = [m[d][3] if d in m else None for d in self.dates]
            self.close[s] = [m[d][4] if d in m else None for d in self.dates]


def simulate(data: Data, signal, capital, start_idx, end_idx, cost_bps=2.0, exposure_cap=None, order_cap=None, costs=None,
             stop_pct=None):
    """signal(i) -> {sym: weight} using data through close i (or None for 'no change').
    Target value = weight x min(equity, exposure_cap); each day's buy per symbol <= order_cap;
    `costs` (config.Costs) adds SEC/TAF fees on sells. `stop_pct`: a broker stop that far below the
    average cost; hit when the day's low reaches it, filled at min(open, stop) less stop slippage, and
    the symbol is not re-bought until the signal changes."""
    cost = cost_bps / 1e4
    settled, pending, hold, want, basis = float(capital), [], {}, {}, {}
    target, trades, eq, expo = {}, 0, [], []
    for i in range(start_idx, end_idx):
        settled += sum(a for k, a in pending if k <= i)
        pending = [(k, a) for k, a in pending if k > i]
        new = signal(i - 1)  # decided at the close of the previous day
        if new is not None and new != target:
            px = {s: data.open[s][i] for s in set(hold) | set(new)}
            if all(v for v in px.values()):  # a missing open retries the change tomorrow
                target = new
                equity = settled + sum(a for _, a in pending) + sum(q * px[s] for s, q in hold.items())
                base = min(equity, exposure_cap or equity)
                want = {s: int(base * w / px[s]) for s, w in target.items() if w > 0}
                for s in list(hold):  # sells first; their cash is usable from the next day (T+1)
                    q = hold[s] - want.get(s, 0)
                    if q > 0:
                        p = px[s] * (1 - cost)
                        pending.append((i + 1, q * p - (sell_fees(q, p, costs) if costs else 0.0)))
                        hold[s] -= q
                        trades += 1
                        if hold[s] == 0:
                            del hold[s]
        for s, w in want.items():  # buys every day until the target is reached, SETTLED cash only
            o = data.open[s][i]
            q = min(w - hold.get(s, 0), int(settled / (o * (1 + cost)))) if o else 0
            if q > 0 and order_cap:
                q = min(q, int(order_cap / (o * (1 + cost))))
            if q > 0:
                settled -= q * o * (1 + cost)
                basis[s] = (basis.get(s, 0) * hold.get(s, 0) + q * o * (1 + cost)) / (hold.get(s, 0) + q)
                hold[s] = hold.get(s, 0) + q
                trades += 1
        for s in list(hold) if stop_pct else []:
            stop, lo_ = basis[s] * (1 - stop_pct / 100), data.low[s][i]
            if lo_ is not None and lo_ <= stop:
                p = min(data.open[s][i], stop) * (1 - cost - (costs.stop_slippage_bps / 1e4 if costs else 0))
                pending.append((i + 1, hold[s] * p - (sell_fees(hold[s], p, costs) if costs else 0.0)))
                del hold[s]
                want.pop(s, None)
                trades += 1
        mv = sum(q * data.close[s][i] for s, q in hold.items() if data.close[s][i])
        total = settled + sum(a for _, a in pending) + mv
        eq.append(total)
        expo.append(mv / total if total else 0)
    return {"equity": eq, "trades": trades, "exposure_pct": 100 * mean(expo) if expo else 0}


def stats(eq, capital, lo=0, hi=None):
    e = eq[lo:hi] if hi else eq[lo:]
    base = e[0]
    peak, mdd = base, 0.0
    for x in e:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / peak)
    yrs = max(len(e) / 252, 1e-9)
    rets = [e[k] / e[k - 1] - 1 for k in range(1, len(e))]
    sd = pstdev(rets) if len(rets) > 1 else 0
    return {"return_pct": round((e[-1] / base - 1) * 100, 1), "cagr_pct": round(((e[-1] / base) ** (1 / yrs) - 1) * 100, 1),
            "max_dd_pct": round(mdd * 100, 1), "end_equity": round(e[-1]), "net_usd": round(e[-1] - base),
            "sharpe": round(mean(rets) / sd * 252 ** 0.5, 2) if sd else 0.0,
            "worst_day_usd": round(min((e[k] - e[k - 1] for k in range(1, len(e))), default=0))}


# ---- pre-registered strategies -----------------------------------------------------------------
SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


def buy_hold(data):
    return lambda i: {"SPY": 1.0}


def trend_200(data):
    c, ma = data.close["SPY"], sma(data.close["SPY"], 200)
    return lambda i: None if ma[i] is None else {"SPY": 1.0 if c[i] > ma[i] else 0.0}


def rsi2_live(closes):
    """D2 for the live runner, on daily closes through the prior session (>= 200):
    enter = close > 200d SMA and RSI(2) < 10; exit = close > 5d SMA. Same math as rsi2_reversion."""
    c = closes[-1]
    return {"enter": c > mean(closes[-200:]) and rsi_wilder(closes, 2)[-1] < 10, "exit": c > mean(closes[-5:])}


def rsi2_reversion(data):
    c = data.close["SPY"]
    ma200, ma5, r = sma(c, 200), sma(c, 5), rsi_wilder(c, 2)
    state = {"in": False}

    def sig(i):
        if ma200[i] is None or r[i] is None:
            return None
        if not state["in"] and c[i] > ma200[i] and r[i] < 10:
            state["in"] = True
        elif state["in"] and c[i] > ma5[i]:
            state["in"] = False
        return {"SPY": 1.0 if state["in"] else 0.0}
    return sig


def sector_momentum(data, top=3):
    avail = [s for s in SECTORS if s in data.close]

    def sig(i):
        if i < 253 or (i + 1 < len(data.dates) and data.dates[i + 1].month == data.dates[i].month):
            return None  # rebalance only on the last trading day of a month
        scored = []
        for s in avail:
            a, b = data.close[s][i - 252], data.close[s][i - 21]
            if a and b:
                scored.append((b / a - 1, s))  # 12-1 month momentum
        scored.sort(reverse=True)
        pick = [s for m, s in scored[:top] if m > 0]
        return {s: 1.0 / top for s in pick} or {"SPY": 0.0}
    return sig
