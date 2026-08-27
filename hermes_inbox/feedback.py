"""Feedback store — the correction loop.

When the agent gets a call wrong you tell it so, and the correction is appended
here as a labeled example. Those examples are injected into the classifier prompt
on every subsequent run, so the same mistake stops recurring.

This is deliberately the whole of the "learning". There is no fine-tuning and no
weight update: corrections are data, the prompt is the model of your preferences,
and `evals.py` is what stops the pile of corrections from silently making things
worse.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .schema import Message


@dataclass(frozen=True)
class Example:
    """One human-labeled message."""

    uid: str
    sender: str
    subject: str
    snippet: str
    label: bool  # True = should have notified me
    note: str = ""
    labeled_at: str = ""

    @classmethod
    def from_message(cls, message: Message, label: bool, note: str = "") -> "Example":
        return cls(
            uid=message.uid,
            sender=message.sender,
            subject=message.subject,
            snippet=message.snippet(300),
            label=label,
            note=note,
            labeled_at=datetime.now(timezone.utc).isoformat(),
        )

    def render(self) -> str:
        verdict = "IMPORTANT" if self.label else "NOT IMPORTANT"
        lines = [
            f"From: {self.sender}",
            f"Subject: {self.subject}",
            f"Body: {self.snippet}",
            f"Correct verdict: {verdict}",
        ]
        if self.note:
            lines.append(f"Why: {self.note}")
        return "\n".join(lines)


class FeedbackStore:
    """Append-only JSONL of labeled examples, newest last."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def add(self, example: Example) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(example), ensure_ascii=False) + "\n")

    def all(self) -> list[Example]:
        if not self.path.is_file():
            return []
        examples: list[Example] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                examples.append(Example(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue  # tolerate a partially written trailing line
        return examples

    def recent(self, limit: int, exclude_uid: str | None = None) -> list[Example]:
        """The newest `limit` examples, optionally omitting one.

        `exclude_uid` exists for leave-one-out evaluation: scoring an example
        while it sits in the prompt measures nothing.
        """
        examples = [e for e in self.all() if e.uid != exclude_uid]
        return examples[-limit:] if limit > 0 else examples

    def counts(self) -> tuple[int, int]:
        examples = self.all()
        important = sum(1 for e in examples if e.label)
        return important, len(examples) - important


def render_examples(examples: Iterable[Example]) -> str:
    """Format labeled examples for the classifier prompt."""
    blocks = [e.render() for e in examples]
    if not blocks:
        return ""
    return "\n\n---\n\n".join(blocks)
