"""Redaction pass.

Runs on every message before any text reaches the model. The point is not
perfect anonymization — it is that predictable high-value secrets (one-time
codes, card numbers, API keys) never leave the machine, even when the classifier
prompt is sent to a hosted provider.

Sender and subject are deliberately preserved: importance is largely a function
of who wrote to you, so redacting the sender would defeat the classifier.
"""

from __future__ import annotations

import re

# Ordered: earlier patterns win, so card numbers are not first eaten by the
# generic long-digit-run rule.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 13–19 digits, optionally split by spaces or hyphens in groups.
    ("[card]", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    # E.164-ish and common national formats.
    ("[phone]", re.compile(r"(?<![\w.])\+?\d{1,3}[ .-]?\(?\d{2,4}\)?[ .-]?\d{3,4}[ .-]?\d{3,4}(?![\w.])")),
    # Provider API keys / bearer tokens: long, high-entropy, prefixed or not.
    ("[token]", re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b")),
    ("[token]", re.compile(r"\b[A-Za-z0-9_-]{40,}\b")),
    # Standalone one-time codes: 4–8 digits on their own.
    ("[code]", re.compile(r"(?<![\w.-])\d{4,8}(?![\w.-])")),
]

_URL_CREDENTIALS = re.compile(r"(https?://[^\s]*?[?&](?:token|key|auth|password|secret)=)[^\s&]+", re.I)


def redact(text: str) -> str:
    """Replace secret-shaped substrings with stable placeholders."""
    if not text:
        return text
    redacted = _URL_CREDENTIALS.sub(r"\1[redacted]", text)
    for placeholder, pattern in _PATTERNS:
        redacted = pattern.sub(placeholder, redacted)
    return redacted


def redact_message(message):
    """Return a copy of `message` with its body redacted.

    Imported lazily by callers that hold a `Message`; kept here so the redaction
    rules live in exactly one place.
    """
    from dataclasses import replace

    return replace(message, body=redact(message.body))
