"""Console notifier — the default when Telegram is not configured."""

from __future__ import annotations

import sys

from ..schema import Decision

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


class ConsoleNotifier:
    name = "console"

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.color = hasattr(self.stream, "isatty") and self.stream.isatty()

    def _fmt(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self.color else text

    def send(self, decision: Decision) -> None:
        message, verdict = decision.message, decision.verdict
        print(self._fmt(f"\n▲ {message.subject or '(no subject)'}", _BOLD), file=self.stream)
        print(f"  from {message.sender}", file=self.stream)
        print(f"  {verdict.reason}", file=self.stream)
        if verdict.suggested_action:
            print(f"  → {verdict.suggested_action}", file=self.stream)
        print(
            self._fmt(
                f"  {verdict.category} · score {verdict.score:.2f} · rule {decision.gate.rule}"
                f" · uid {message.uid}",
                _DIM,
            ),
            file=self.stream,
        )
        self.stream.flush()

    def poll_feedback(self, offset: int | None = None):
        return [], offset
