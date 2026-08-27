"""Durable run state: how far we have read, and what we already decided.

Kept as plain JSON/JSONL so it can be inspected, edited, and version-skipped
without a migration story.
"""

from __future__ import annotations

import json
from pathlib import Path

from .schema import Decision


class State:
    """Last-seen UID per source, plus the Telegram update offset."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict = {}
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}

    def last_uid(self, source: str) -> str | None:
        return self._data.get("last_uid", {}).get(source)

    def set_last_uid(self, source: str, uid: str) -> None:
        self._data.setdefault("last_uid", {})[source] = str(uid)

    @property
    def telegram_offset(self) -> int | None:
        return self._data.get("telegram_offset")

    @telegram_offset.setter
    def telegram_offset(self, value: int | None) -> None:
        self._data["telegram_offset"] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")


class DecisionLog:
    """Append-only record of every pass, so corrections can refer back to one."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def append(self, decision: Decision) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(decision.to_dict(), ensure_ascii=False) + "\n")

    def all(self) -> list[Decision]:
        if not self.path.is_file():
            return []
        out: list[Decision] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Decision.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return out

    def find(self, uid: str) -> Decision | None:
        """Most recent decision for a message uid."""
        for decision in reversed(self.all()):
            if decision.message.uid == uid:
                return decision
        return None
