---
name: desk
description: Run one meeting of the six-bot paper-trading desk (Scanner, News, Entry/Exit, Risk, Skeptic, Desk Lead) inside Claude Code, no API. Use for "/desk", "run the desk", or on a loop with /loop 30m /desk. Paper Alpaca only.
---

One desk meeting. All commands run from `claude_desk/` in the repo (`cd` there first). Every bot message goes to the monitor UI via `post`.

1. `python desk_tools.py snapshot`. If output says MARKET CLOSED or LAST 20 MINUTES, report it in one line and stop.
2. Read `claude_desk/prompts.json`. Play each role in order, reading the whole chat so far (snapshot output plus earlier bot messages). After each message run `python desk_tools.py post "<NAME>"` with the text on stdin (heredoc). Names: SCANNER, NEWS, ENTRY/EXIT, RISK, SKEPTIC, DESK LEAD.
   - NEWS: use the snapshot HEADLINES plus WebSearch. Never invent news.
   - SKEPTIC: spawn a fresh Agent (general-purpose, model sonnet) with the skeptic prompt and the chat so far, so it argues without this session's bias. It may use WebSearch.
   - SCANNER with nothing unusual: post NOTHING UNUSUAL, skip the remaining bots, post a DESK LEAD line ending `TRADES:` and `[]`.
3. Pipe the Desk Lead message (must end `TRADES:` then JSON list) to `python desk_tools.py execute`. Code sizes positions (0.5% risk, 10% cap) and only places orders if `AUTO_PAPER=true` in `.env`. Never change `size()` or the paper URL.
4. Reply with 2 lines: what the desk decided, and whether any order was placed.

Monitor UI: `python desk_tools.py serve` then open http://localhost:8765. Schedule: `/loop 30m /desk`.
