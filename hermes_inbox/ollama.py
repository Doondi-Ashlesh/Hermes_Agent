"""Ollama provider — local models, no API bill, no data leaving the machine.

Same signature as `classify.classify`, so it drops straight into `classify_fn`.

Two things degrade relative to a frontier model, and both matter here:

- **Few-shot adherence.** The correction loop *is* in-context learning. A small
  model follows 40 labeled examples that contradict its standing instructions
  less reliably, so the "learns from your corrections" property is the first
  thing to weaken.
- **Injection resistance.** `fixtures/inbox.json` uid 109 is an email that tries
  to instruct the classifier. Small models comply with that more often.

Neither is a reason not to use it — they are reasons to run `hermes-inbox eval`
against your own mail and see the numbers rather than take anyone's word for it.

Requires Ollama running locally with structured outputs (0.5+):

    ollama serve
    ollama pull qwen2.5:7b
    HERMES_PROVIDER=ollama HERMES_OLLAMA_MODEL=qwen2.5:7b hermes-inbox once
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .classify import SCHEMA, SYSTEM, build_system, build_user
from .redact import redact_message
from .schema import Message, Verdict


class OllamaError(RuntimeError):
    pass


def _system_text(examples) -> str:
    """Flatten the cacheable blocks into one string — Ollama has no cache API."""
    return "\n\n".join(block["text"] for block in build_system(examples))


def classify(message: Message, examples=None, config=None, client=None) -> Verdict:
    from .config import Config

    config = config or Config()
    examples = examples or []
    safe = redact_message(message)

    payload = {
        "model": config.ollama_model,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": _system_text(examples)},
            {"role": "user", "content": build_user(safe)},
        ],
    }

    request = urllib.request.Request(
        config.ollama_host.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=config.ollama_timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise OllamaError(f"ollama returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OllamaError(
            f"cannot reach ollama at {config.ollama_host} ({exc.reason}) — is `ollama serve` running?"
        ) from exc

    content = (body.get("message") or {}).get("content", "")
    if not content:
        raise OllamaError(f"ollama returned no content: {body}")

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"ollama did not return valid JSON: {content[:200]}") from exc

    # Small models sometimes emit an out-of-range score or an unlisted category
    # even under a schema; clamp rather than crash the polling loop.
    data["score"] = max(0.0, min(1.0, float(data.get("score", 0.0))))
    if data.get("category") not in SCHEMA["properties"]["category"]["enum"]:
        data["category"] = "other"
    data.setdefault("suggested_action", "")
    data.setdefault("reason", "")
    data["important"] = bool(data.get("important", data["score"] >= 0.5))
    return Verdict.from_dict(data)


__all__ = ["classify", "OllamaError", "SYSTEM"]
