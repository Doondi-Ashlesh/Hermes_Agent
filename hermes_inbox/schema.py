"""Normalized message schema.

Everything downstream of an ingestion adapter sees a `Message` and nothing else.
Swapping IMAP for the Gmail API, or for a ticketing system, must not require a
change to the classifier, the gate, or the notifiers.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Message:
    """One inbound message, normalized away from its source's wire format."""

    uid: str
    source: str
    sender: str
    subject: str
    body: str
    received_at: datetime
    sender_name: str = ""
    folder: str = "INBOX"
    headers: dict[str, str] = field(default_factory=dict)

    def snippet(self, limit: int = 600) -> str:
        """Collapsed body text, truncated for prompts and notifications."""
        collapsed = re.sub(r"\s+", " ", self.body).strip()
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["received_at"] = self.received_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        data = dict(data)
        received = data.get("received_at")
        if isinstance(received, str):
            data["received_at"] = datetime.fromisoformat(received)
        elif received is None:
            data["received_at"] = _utcnow()
        return cls(**data)


@dataclass(frozen=True)
class Verdict:
    """The model's opinion about a message. Advisory — the gate decides."""

    important: bool
    score: float
    category: str
    reason: str
    suggested_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Verdict":
        return cls(
            important=bool(data["important"]),
            score=float(data["score"]),
            category=str(data["category"]),
            reason=str(data["reason"]),
            suggested_action=str(data.get("suggested_action", "")),
        )


@dataclass(frozen=True)
class GateDecision:
    """The deterministic outcome. `rule` records which rule fired, always."""

    notify: bool
    rule: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Decision:
    """A full pass over one message: what the model said, what the gate did."""

    message: Message
    verdict: Verdict
    gate: GateDecision
    decided_at: datetime = field(default_factory=_utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message.to_dict(),
            "verdict": self.verdict.to_dict(),
            "gate": self.gate.to_dict(),
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        return cls(
            message=Message.from_dict(data["message"]),
            verdict=Verdict.from_dict(data["verdict"]),
            gate=GateDecision(**data["gate"]),
            decided_at=datetime.fromisoformat(data["decided_at"]),
        )
