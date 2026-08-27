"""Notifier interface.

`send` delivers one alert. `poll_feedback` drains any corrections the human made
on previous alerts and returns them as `(message_uid, is_important)` pairs — this
is the seam that closes the learning loop, so a channel that cannot carry a reply
should return an empty list rather than raise.
"""

from __future__ import annotations

from typing import Protocol

from ..schema import Decision


class Notifier(Protocol):
    name: str

    def send(self, decision: Decision) -> None: ...

    def poll_feedback(
        self, offset: int | None = None
    ) -> tuple[list[tuple[str, bool]], int | None]: ...
