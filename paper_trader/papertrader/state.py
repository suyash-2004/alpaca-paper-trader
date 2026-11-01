"""Small persisted state: duplicate-order ids, plans of runner-owned positions, day halts."""
import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, path):
        self.path = Path(path)
        self.data = {"sent_ids": [], "plans": {}, "halts": {}, "foreign": [], "journaled": []}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # duplicate protection: an id is recorded BEFORE the order call, so a crash mid-call
    # can never lead to the same signal being sent twice.
    def seen(self, client_order_id):
        return client_order_id in self.data["sent_ids"]

    def mark_sent(self, client_order_id):
        self.data["sent_ids"].append(client_order_id)
        self.data["sent_ids"] = self.data["sent_ids"][-5000:]
        self.save()
