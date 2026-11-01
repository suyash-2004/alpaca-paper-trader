"""desk_tools.py - no-LLM helper for the Claude Code paper-trading desk.
  snapshot      print market data + headlines for this round (flattens near close if AUTO_PAPER)
  post NAME     read text on stdin, append it to desk_log.jsonl (the UI tails this)
  execute       read Desk Lead message on stdin, size each BUY in code, place paper order or log idea
  serve [port]  local monitor UI at http://localhost:8765
Paper Alpaca only. Needs .env (see .env.example)."""
import os, re, sys, json, math, datetime, requests
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, ".env"))
LOG = os.path.join(BASE, "desk_log.jsonl")
AUTO_PAPER = os.getenv("AUTO_PAPER", "false").lower() == "true"
RISK_PCT = float(os.getenv("RISK_PCT", "0.5"))        # max % of account lost per trade
MAX_POS_PCT = float(os.getenv("MAX_POS_PCT", "10"))   # max % of account in one stock
PAPER = "https://paper-api.alpaca.markets"  # paper only, never the live URL
DATA = "https://data.alpaca.markets"


def hdr():
    return {"APCA-API-KEY-ID": os.environ["ALPACA_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET"]}


def alp(method, url, **kw):
    r = requests.request(method, url, headers=hdr(), timeout=30, **kw)
    if not r.ok:
        raise RuntimeError(f"Alpaca said {r.status_code}: {r.text[:200]} (use PAPER keys)")
    return r.json() if r.text else {}


def post(name, text):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": datetime.datetime.now().isoformat(timespec="seconds"),
                            "name": name, "text": text}) + "\n")
    print(f"[{name}] {text}")


def size(entry, stop, equity):
    """Hard money rules in plain code; no bot or prompt can override them."""
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    by_risk = (equity * RISK_PCT / 100) / risk_per_share
    by_cap = (equity * MAX_POS_PCT / 100) / entry
    return max(0, math.floor(min(by_risk, by_cap)))


def held_symbols():
    pos = alp("GET", f"{PAPER}/v2/positions")
    ords = alp("GET", f"{PAPER}/v2/orders", params={"status": "open"})
    return {p["symbol"] for p in pos} | {o["symbol"] for o in ords}


def snapshot():
    c = alp("GET", f"{PAPER}/v2/clock")
    now = datetime.datetime.fromisoformat(re.sub(r"(\.\d+)", "", c["timestamp"]))
    close = datetime.datetime.fromisoformat(re.sub(r"(\.\d+)", "", c["next_close"]))
    mins = (close - now).total_seconds() / 60
    if not c["is_open"]:
        print(f"MARKET CLOSED ({now:%Y-%m-%d %H:%M} New York). Stop the round.")
        return
    if mins <= 20:
        if AUTO_PAPER:
            alp("DELETE", f"{PAPER}/v2/positions", params={"cancel_orders": "true"})
            post("SYSTEM", "Near the close: all paper positions closed, open orders cancelled.")
        print(f"LAST 20 MINUTES ({mins:.0f} left). No new trades. Stop the round.")
        return
    equity = float(alp("GET", f"{PAPER}/v2/account")["equity"])
    act = alp("GET", f"{DATA}/v1beta1/screener/stocks/most-actives", params={"by": "volume", "top": 30})
    syms = [a["symbol"] for a in act["most_actives"]]
    snaps = alp("GET", f"{DATA}/v2/stocks/snapshots", params={"symbols": ",".join(syms)})
    print(f"New York time: {now:%Y-%m-%d %H:%M}. Minutes until close: {mins:.0f}. Paper account value: ${equity:,.0f}.")
    print("\nMARKET DATA\nsymbol | price | change % | volume vs yesterday")
    for s in syms:
        sn = snaps.get(s) or {}
        d, p = sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
        if d and p and p.get("v"):
            print(f"{s} | {d['c']:.2f} | {(d['c'] - p['c']) / p['c'] * 100:+.1f}% | {d['v'] / p['v']:.2f}x")
    print("\nHEADLINES")
    for a in alp("GET", f"{DATA}/v1beta1/news", params={"symbols": ",".join(syms), "limit": 30}).get("news", []):
        print(f"{','.join(a['symbols'][:3])} | {a['created_at'][:16]} | {a['headline']} ({a['source']})")
    print("\nHELD OR OPEN ORDERS:", ", ".join(sorted(held_symbols())) or "none")


def execute(msg):
    try:
        tail = msg[msg.rindex("TRADES:") + 7:]
        plan = json.loads(tail[tail.index("["): tail.rindex("]") + 1])
    except ValueError:
        return post("SYSTEM", "No valid trade list from the Desk Lead. Nothing done this round.")
    equity = float(alp("GET", f"{PAPER}/v2/account")["equity"])
    held = held_symbols()
    for t in plan:
        if t.get("action") != "BUY":
            continue
        if t["symbol"] in held:
            post("SYSTEM", f"{t['symbol']}: already held or has an open order, skipped.")
            continue
        held.add(t["symbol"])  # never two orders for one stock in one round
        qty = size(float(t["entry"]), float(t["stop"]), equity)
        line = f"{t['symbol']}: buy {qty} @ {t['entry']} stop {t['stop']} target {t['target']}"
        if qty < 1:
            post("SYSTEM", f"{t['symbol']}: size rounds to 0 shares, skipped.")
        elif AUTO_PAPER:
            order = {"symbol": t["symbol"], "qty": str(qty), "side": "buy", "type": "limit",
                     "limit_price": str(round(float(t["entry"]), 2)), "time_in_force": "day",
                     "order_class": "bracket",
                     "take_profit": {"limit_price": str(round(float(t["target"]), 2))},
                     "stop_loss": {"stop_price": str(round(float(t["stop"]), 2))}}
            r = requests.post(f"{PAPER}/v2/orders", headers=hdr(), json=order, timeout=30)
            post("SYSTEM", f"PAPER ORDER {line} -> {r.status_code} {r.text[:200]}")
        else:
            post("SYSTEM", f"IDEA ONLY (AUTO_PAPER=false): {line}")


class UI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else body.encode())

    def do_GET(self):
        if self.path == "/log":
            rows = open(LOG, encoding="utf-8").read().splitlines() if os.path.exists(LOG) else []
            self.send("[" + ",".join(rows[-200:]) + "]", "application/json")
        elif self.path == "/account":
            try:
                a = alp("GET", f"{PAPER}/v2/account")
                pos = [{k: p[k] for k in ("symbol", "qty", "avg_entry_price", "unrealized_pl")}
                       for p in alp("GET", f"{PAPER}/v2/positions")]
                out = {"equity": a["equity"], "pl_today": float(a["equity"]) - float(a["last_equity"]),
                       "auto_paper": AUTO_PAPER, "positions": pos}
            except Exception as e:
                out = {"error": str(e)}
            self.send(json.dumps(out), "application/json")
        else:
            self.send(open(os.path.join(BASE, "index.html"), "rb").read(), "text/html; charset=utf-8")


def demo():
    assert size(100, 98, 100_000) == 100   # risk cap 250, position cap 100 wins
    assert size(100, 90, 10_000) == 5      # risk cap $50 / $10 = 5
    assert size(100, 101, 10_000) == 0     # stop above entry
    print("size() ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "snapshot":
        snapshot()
    elif cmd == "post":
        post(sys.argv[2], sys.stdin.read().strip())
    elif cmd == "execute":
        execute(sys.stdin.read())
    elif cmd == "serve":
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
        print(f"Desk monitor: http://localhost:{port}")
        HTTPServer(("127.0.0.1", port), UI).serve_forever()
    elif cmd == "demo":
        demo()
    else:
        print(__doc__)
