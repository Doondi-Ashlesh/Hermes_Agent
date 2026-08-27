"""Mail source interface.

The only thing in the system that knows how a specific provider works. Anything
that implements `fetch_new` can be dropped in without touching agent code.
"""

from __future__ import annotations

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
