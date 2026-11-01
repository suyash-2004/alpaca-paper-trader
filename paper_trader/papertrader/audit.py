"""Structured JSONL audit log with secrets redacted, plus alerts."""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_SECRET_KEY_RE = re.compile(r"key|secret|token|password|authorization|credential", re.I)
REDACTED = "[REDACTED]"


class Audit:
    def __init__(self, directory, secrets=(), echo=True):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in secrets if s]
        self.echo = echo

    def _scrub(self, obj):
        if isinstance(obj, dict):
            return {k: (REDACTED if _SECRET_KEY_RE.search(str(k)) else self._scrub(v)) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._scrub(v) for v in obj]
        if isinstance(obj, str):
            for s in self.secrets:
                obj = obj.replace(s, REDACTED)
            return obj
        return obj

    def _write(self, name, record):
        line = json.dumps(self._scrub(record), default=str, sort_keys=True)
        with open(self.dir / name, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return line

    def log(self, event, **data):
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        self._write("audit.jsonl", rec)

    def alert(self, msg, **data):
        """Errors and unexpected states: audit log + alerts.log + stderr."""
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": "ALERT", "msg": msg, **data}
        line = self._write("alerts.log", rec)
        self._write("audit.jsonl", rec)
        if self.echo:
            print("ALERT:", self._scrub(msg), file=sys.stderr)
        return line

    def journal(self, record):
        """Closed-trade journal used for the paper-forward report."""
        self._write("trades.jsonl", record)
