"""Fixture source — a mailbox in a JSON file.

Lets the whole pipeline run with no credentials and no network to the mail
provider, which is what makes `hermes-inbox demo` and the test suite possible.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..schema import Message


class FixtureSource:
    name = "fixtures"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> list[Message]:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [Message.from_dict({**item, "source": self.name}) for item in raw]

    def fetch_new(self, since_uid: str | None = None, limit: int = 25) -> list[Message]:
        messages = self._load()
        if since_uid is not None:
            messages = [m for m in messages if int(m.uid) > int(since_uid)]
        return messages[:limit]

    def fetch_since(self, since, limit: int = 500) -> list[Message]:
        return [m for m in self._load() if m.received_at >= since][:limit]
