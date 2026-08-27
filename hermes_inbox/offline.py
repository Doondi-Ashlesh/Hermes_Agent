"""Offline stand-in classifier.

Keyword heuristics, no model call. This exists so `hermes-inbox demo` runs with
no credentials and so the pipeline can be exercised in CI — it is NOT the
product. It has no notion of context, cannot read tone, and will not improve
from your corrections; the real classifier is in `classify.py`.

Kept deliberately simple and readable so nobody mistakes it for the real thing.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .schema import Message, Verdict

_BULK_HEADERS = ("list-unsubscribe", "precedence", "auto-submitted")

_SIGNALS: list[tuple[str, float, str, re.Pattern[str]]] = [
    (
        "security",
        0.95,
        "Mentions account security or an unrecognized sign-in.",
        re.compile(r"\b(sign-?in|password|breach|unauthori[sz]ed|rotate|2fa|verify your account)\b", re.I),
    ),
    (
        "billing",
        0.9,
        "Payment or billing action appears to be required.",
        re.compile(r"\b(payment failed|past due|invoice|suspend(ed|ing)?|card (was )?declined)\b", re.I),
    ),
    (
        "personal",
        0.8,
        "A person appears to be waiting on a reply or a decision.",
        re.compile(r"\b(sign-?off|by (thursday|friday|monday|tomorrow|eod)|are you free|can you send|need your)\b", re.I),
    ),
    (
        "promotion",
        0.05,
        "Marketing copy with manufactured urgency.",
        re.compile(r"\b(\d{1,3}% off|final hours|shop now|act now|biggest sale|free shipping)\b", re.I),
    ),
    (
        "automated",
        0.1,
        "Automated delivery or digest notification.",
        re.compile(r"\b(undelivered|mail delivery|daily digest|returned to sender|rsvp)\b", re.I),
    ),
]

_INJECTION = re.compile(
    r"(instruction|note)s? (for|to) (email )?(assistants?|ai|agents?|bots?)"
    r"|classify (this|it) as|ignore (your|all|previous)|mark (this|it) (as )?(important|urgent)",
    re.I,
)


def has_credentials() -> bool:
    """Whether an Anthropic credential is resolvable without prompting."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def classify(message: Message, examples=None, config=None, client=None) -> Verdict:
    """Score a message with keyword rules. Signature matches `classify.classify`."""
    haystack = f"{message.subject}\n{message.body}"

    if _INJECTION.search(haystack):
        return Verdict(
            important=False,
            score=0.02,
            category="spam",
            reason="Contains text trying to instruct an email assistant — treated as a spam signal.",
            suggested_action="",
        )

    bulk = any(h.lower() in _BULK_HEADERS for h in message.headers)

    best: tuple[float, str, str] | None = None
    for category, score, reason, pattern in _SIGNALS:
        if pattern.search(haystack):
            if best is None or score > best[0]:
                best = (score, category, reason)

    if best is None:
        score, category, reason = (
            (0.15, "newsletter", "No actionable signal; looks like subscribed bulk mail.")
            if bulk
            else (0.4, "other", "No strong signal either way.")
        )
    else:
        score, category, reason = best
        if bulk and score > 0.5:
            score -= 0.3
            reason += " Sent as bulk mail, so weighted down."

    return Verdict(
        important=score >= 0.5,
        score=round(score, 2),
        category=category,
        reason=reason,
        suggested_action="review" if score >= 0.5 else "",
    )
