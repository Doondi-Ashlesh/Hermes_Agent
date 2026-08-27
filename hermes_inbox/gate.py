"""The policy gate.

Deterministic code. The model produces a `Verdict`; this decides whether you get
pinged. Every rule is expressible without the model, every decision records which
rule fired, and the rules are ordered so that a human's explicit instruction
always beats a model score.

Rule order is load-bearing:

1. never-senders   — an explicit mute always wins, whatever the model thinks
2. always-senders  — an explicit escalation beats a low score
3. always-keywords — subject-line triggers you set by hand
4. muted category  — whole classes of mail you never want pinged about
5. quiet hours     — time-of-day suppression
6. threshold       — only now does the model's score get consulted
"""

from __future__ import annotations

from datetime import datetime, timezone

from .config import GateConfig
from .schema import GateDecision, Message, Verdict


def _addr(message: Message) -> str:
    return message.sender.strip().lower()


def _matches(addr: str, patterns: list[str]) -> str | None:
    """Return the pattern that matches, or None.

    A pattern is either a full address (`a@b.com`) or a bare domain (`b.com`),
    which matches any address on that domain.
    """
    for pattern in patterns:
        if not pattern:
            continue
        if addr == pattern or addr.endswith("@" + pattern.lstrip("@")):
            return pattern
    return None


def _in_quiet_hours(now: datetime, start: int | None, end: int | None) -> bool:
    if start is None or end is None:
        return False
    hour = now.astimezone().hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # Window wraps midnight, e.g. 22-7.
    return hour >= start or hour < end


def decide(
    message: Message,
    verdict: Verdict,
    config: GateConfig,
    now: datetime | None = None,
) -> GateDecision:
    """Apply the rules in order and return the first that fires."""
    now = now or datetime.now(timezone.utc)
    addr = _addr(message)

    muted = _matches(addr, config.never_senders)
    if muted:
        return GateDecision(notify=False, rule=f"never-sender:{muted}")

    escalated = _matches(addr, config.always_senders)
    if escalated:
        return GateDecision(notify=True, rule=f"always-sender:{escalated}")

    haystack = f"{message.subject} {message.snippet(200)}".lower()
    for keyword in config.always_keywords:
        if keyword and keyword in haystack:
            return GateDecision(notify=True, rule=f"always-keyword:{keyword}")

    if verdict.category.lower() in config.muted_categories:
        return GateDecision(notify=False, rule=f"muted-category:{verdict.category}")

    if _in_quiet_hours(now, config.quiet_start, config.quiet_end):
        return GateDecision(notify=False, rule="quiet-hours")

    if verdict.score >= config.threshold:
        return GateDecision(notify=True, rule=f"score>={config.threshold:g}")

    return GateDecision(notify=False, rule=f"score<{config.threshold:g}")
