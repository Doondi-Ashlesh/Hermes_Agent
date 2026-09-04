"""Mail source interface.

The only thing in the system that knows how a specific provider works. Anything
that implements `fetch_new` can be dropped in without touching agent code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..schema import Message


class MailSource(Protocol):
    name: str

    def fetch_new(self, since_uid: str | None = None, limit: int = 25) -> list[Message]:
        """Return messages newer than `since_uid`, oldest first.

        Must not mutate server-side state — in particular, must not mark
        messages as read.
        """
        ...

    def fetch_since(self, since: datetime, limit: int = 500) -> list[Message]:
        """Return messages received on or after `since`, oldest first.

        Optional. Only `backfill` uses it, and it is a separate method because
        the live loop walks forward by uid while backfill walks backward by
        date — the same cursor cannot serve both.

        A source without a date query should not implement it; `backfill` will
        report that clearly rather than silently returning nothing.
        """
        ...
