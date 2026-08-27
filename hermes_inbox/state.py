"""Durable run state: how far we have read, and what we already decided.

Plain JSON and JSONL so it can be inspected, edited and diffed without tooling.
That property is worth keeping, so the decision log stays a text file and gets an
in-memory offset index rather than being moved into a database.

Two failure modes drove the current shape:

- `State.save` is atomic (write a temp file, then rename). A crash mid-write used
  to be able to leave a truncated `state.json`, which reads back as "no cursor"
  and re-notifies the entire mailbox.
- `DecisionLog.find` used to parse the whole file on every call, which is once
  per correction. At a year of mail (~36k decisions, ~34 MB) that was ~1s each.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator

from .schema import Decision

# The first "uid" in a serialized Decision is always message.uid — cheaper than
# parsing the whole line when all we need is the key.
_UID = re.compile(rb'"uid":\s*"([^"]*)"')


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    rename(2) is atomic within a filesystem, so a reader either sees the old
    file or the new one — never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class State:
    """Last-seen UID per source, plus the notifier's update offset."""

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
        _atomic_write(self.path, json.dumps(self._data, indent=2))


class DecisionLog:
    """Append-only record of every pass, indexed by message uid.

    The index is built once per process by scanning offsets (no JSON parsing)
    and updated on each append, so `find` is a seek rather than a full scan.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._index: dict[str, int] | None = None

    # -- writing ----------------------------------------------------------- #

    def append(self, decision: Decision) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(decision.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
        offset = self.path.stat().st_size if self.path.is_file() else 0
        with self.path.open("ab") as fh:
            fh.write(line)
        if self._index is not None:
            self._index[decision.message.uid] = offset

    # -- indexing ---------------------------------------------------------- #

    def _build_index(self) -> dict[str, int]:
        index: dict[str, int] = {}
        if not self.path.is_file():
            return index
        offset = 0
        with self.path.open("rb") as fh:
            for raw in fh:
                match = _UID.search(raw)
                if match:
                    # Later entries win: `find` returns the most recent decision.
                    index[match.group(1).decode("utf-8")] = offset
                offset += len(raw)
        return index

    def _read_at(self, offset: int) -> dict | None:
        with self.path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.readline()
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # -- reading ----------------------------------------------------------- #

    def find(self, uid: str) -> Decision | None:
        """Most recent decision for a message uid, or None."""
        for attempt in range(2):
            if self._index is None:
                self._index = self._build_index()
            offset = self._index.get(uid)
            if offset is None:
                if attempt == 0 and self.path.is_file():
                    self._index = None  # index may predate an external append
                    continue
                return None
            data = self._read_at(offset)
            if data and data.get("message", {}).get("uid") == uid:
                try:
                    return Decision.from_dict(data)
                except (KeyError, TypeError, ValueError):
                    return None
            self._index = None  # stale offset — rebuild and retry once
        return None

    def iter_all(self) -> Iterator[Decision]:
        """Stream every decision. Use this rather than `all()` over long logs."""
        if not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield Decision.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue

    def all(self) -> list[Decision]:
        return list(self.iter_all())
