"""IMAP source.

Works against Gmail (with an app password), Fastmail, Proton Bridge, and any
other IMAP server — no OAuth app registration, no provider review.

Read-only by construction:
- the mailbox is selected with `readonly=True`
- bodies are fetched with `BODY.PEEK[]`, which does not set the \\Seen flag

So running this against a live mailbox cannot alter it. The account credential
should still be an app password scoped to mail, never your main password.
"""

from __future__ import annotations

import email
import imaplib
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from ..schema import Message

_KEEP_HEADERS = ("List-Unsubscribe", "Auto-Submitted", "Precedence", "In-Reply-To")


def _decode(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return raw


def _body_text(msg: EmailMessage) -> str:
    """Prefer text/plain; fall back to stripped HTML."""
    try:
        part = msg.get_body(preferencelist=("plain",))
        if part is not None:
            return part.get_content()
    except (KeyError, AttributeError):
        pass
    try:
        part = msg.get_body(preferencelist=("html",))
        if part is not None:
            import re

            html = part.get_content()
            html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
            return re.sub(r"<[^>]+>", " ", html)
    except (KeyError, AttributeError):
        pass
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload or "")


def _received_at(msg: EmailMessage) -> datetime:
    try:
        parsed = parsedate_to_datetime(msg.get("Date"))
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    return datetime.now(timezone.utc)


class ImapSource:
    name = "imap"

    def __init__(self, host: str, port: int, user: str, password: str, folder: str = "INBOX"):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.folder = folder

    def fetch_since(self, since: datetime, limit: int = 500) -> list[Message]:
        """Messages received on or after `since`, oldest first.

        IMAP's SINCE has day granularity and filters on the server's internal
        date, so the window can be a little wider than asked for. Backfill
        deduplicates against the decision log, which makes that harmless.
        """
        return self._fetch(f'(SINCE {since.strftime("%d-%b-%Y")})', limit)

    def fetch_new(self, since_uid: str | None = None, limit: int = 25) -> list[Message]:
        criteria = f"(UID {int(since_uid) + 1}:*)" if since_uid else "(ALL)"
        return self._fetch(criteria, limit, drop_upto=since_uid)

    def _fetch(self, criteria: str, limit: int, drop_upto: str | None = None) -> list[Message]:
        with imaplib.IMAP4_SSL(self.host, self.port) as conn:
            conn.login(self.user, self.password)
            conn.select(self.folder, readonly=True)

            status, data = conn.uid("SEARCH", None, criteria)
            if status != "OK" or not data or not data[0]:
                return []

            uids = data[0].split()
            # An open-ended `n:*` range always returns at least the last UID even
            # when nothing is newer; drop anything we have already seen.
            if drop_upto:
                uids = [u for u in uids if int(u) > int(drop_upto)]
            uids = uids[-limit:]

            messages: list[Message] = []
            for uid in uids:
                status, payload = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                parsed = email.message_from_bytes(payload[0][1], policy=email.policy.default)
                name, addr = parseaddr(_decode(parsed.get("From")))
                messages.append(
                    Message(
                        uid=uid.decode(),
                        source=self.name,
                        sender=addr.lower(),
                        sender_name=name,
                        subject=_decode(parsed.get("Subject")),
                        body=_body_text(parsed),
                        received_at=_received_at(parsed),
                        folder=self.folder,
                        headers={h: _decode(parsed.get(h)) for h in _KEEP_HEADERS if parsed.get(h)},
                    )
                )
            return messages
