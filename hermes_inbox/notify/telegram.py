"""Telegram notifier.

Chosen over WhatsApp for the first implementation because a bot token takes two
minutes from @BotFather, with no business verification and no message templates
to get approved. `Notifier` is the seam — a WhatsApp implementation drops in
behind it without touching anything upstream.

Each alert carries two inline buttons. Pressing one writes a labeled example to
the feedback store, which is what closes the correction loop: you answer the
notification, and the next classification has your answer in its prompt.

Uses urllib so the package needs no HTTP dependency.
"""

from __future__ import annotations

from ..http import HttpError, post_json
from ..logs import get_logger
from ..schema import Decision

log = get_logger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, timeout: int = 20, retries: int = 3, backoff: float = 0.5):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def _call(self, method: str, payload: dict) -> dict:
        try:
            body = post_json(
                API.format(token=self.token, method=method),
                payload,
                timeout=self.timeout,
                retries=self.retries,
                backoff=self.backoff,
            )
        except HttpError as exc:
            raise TelegramError(f"{method} failed: {exc}") from exc
        # Telegram reports application errors in a 200 body, so this is not
        # something the HTTP layer can retry for us.
        if not body.get("ok"):
            raise TelegramError(f"{method} failed: {body.get('description')}")
        return body.get("result", {})

    def send(self, decision: Decision) -> None:
        message, verdict = decision.message, decision.verdict
        lines = [
            f"*{_escape(message.subject or '(no subject)')}*",
            f"from `{_escape(message.sender)}`",
            "",
            _escape(verdict.reason),
        ]
        if verdict.suggested_action:
            lines.append(f"→ _{_escape(verdict.suggested_action)}_")
        lines.append("")
        lines.append(
            f"`{verdict.category}` · score {verdict.score:.2f} · rule `{_escape(decision.gate.rule)}`"
        )

        self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": "\n".join(lines),
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Right call", "callback_data": f"ok:{message.uid}"},
                            {"text": "🔕 Not important", "callback_data": f"no:{message.uid}"},
                        ]
                    ]
                },
            },
        )

    def poll_feedback(self, offset: int | None = None) -> tuple[list[tuple[str, bool]], int | None]:
        """Drain button presses.

        Returns `(labels, next_offset)` where each label is `(message_uid, is_important)`.
        """
        payload: dict = {"timeout": 0, "allowed_updates": ["callback_query"]}
        if offset is not None:
            payload["offset"] = offset

        updates = self._call("getUpdates", payload)
        labels: list[tuple[str, bool]] = []
        next_offset = offset

        for update in updates:
            next_offset = update["update_id"] + 1
            query = update.get("callback_query")
            if not query:
                continue
            action, _, uid = (query.get("data") or "").partition(":")
            if action in ("ok", "no") and uid:
                labels.append((uid, action == "ok"))
            try:
                self._call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": query["id"],
                        "text": "Noted — thanks." if action == "ok" else "Noted, I'll stop flagging these.",
                    },
                )
            except TelegramError:
                pass  # acknowledgement is cosmetic; never lose the label over it

        return labels, next_offset


_MARKDOWN_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"


def _escape(text: str) -> str:
    return "".join("\\" + ch if ch in _MARKDOWN_V2_SPECIALS else ch for ch in text or "")
