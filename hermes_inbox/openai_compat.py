"""OpenAI-compatible provider — any server that speaks `/v1/chat/completions`.

The main target is self-hosted inference: vLLM, or an NVIDIA NIM container, on
your own GPU. That keeps the property Ollama has — mail never leaves hardware
you control — while running on a serving stack built for throughput, which is
what `eval --concurrency` and `backfill --concurrency` are waiting on.

Same signature as `classify.classify`, so it drops straight into `classify_fn`.

Output is constrained with `response_format: {"type": "json_schema", ...}`, the
OpenAI request shape that vLLM documents for structured outputs. Constrained
decoding guarantees the JSON parses; it does not guarantee the values are sane,
so the result still goes through `coerce_verdict`.

The same caveats as Ollama apply, and they are the reason `eval --compare`
exists: an 8B model follows 40 conflicting corrections less reliably than a
frontier model, and complies with the injection fixture more often. Measure it
on your own labels before switching.

    vllm serve Qwen/Qwen2.5-7B-Instruct            # listens on :8000
    HERMES_PROVIDER=openai-compat hermes-inbox eval --golden

`HERMES_OPENAI_MODEL` may be left empty: a vLLM or NIM server serves one model,
and it is discovered from `/v1/models` on first use.
"""

from __future__ import annotations

import json
import threading

from .classify import SCHEMA, build_user, coerce_verdict, system_text
from .http import HttpError, get_json, post_json
from .logs import get_logger
from .redact import redact_message
from .schema import Message, Verdict

log = get_logger(__name__)


class OpenAICompatError(RuntimeError):
    pass


_discovered: dict[str, str] = {}
_lock = threading.Lock()


def _headers(config) -> dict[str, str]:
    # Local vLLM and NIM run without a key by default; send one only if set.
    return {"Authorization": f"Bearer {config.openai_api_key}"} if config.openai_api_key else {}


def _base(config) -> str:
    return config.openai_base_url.rstrip("/")


def resolve_model(config) -> str:
    """The configured model, or the one the server says it is serving.

    Cached per base URL, and discovered under a lock so a concurrent eval makes
    one `/models` call rather than one per worker.
    """
    if config.openai_model:
        return config.openai_model
    base = _base(config)
    with _lock:
        if base in _discovered:
            return _discovered[base]
        try:
            body = get_json(
                base + "/models",
                timeout=min(config.openai_timeout, 30),
                retries=config.http_retries,
                backoff=config.http_backoff,
                headers=_headers(config),
            )
        except HttpError as exc:
            raise OpenAICompatError(
                f"cannot list models at {base} ({exc}) — is the server running?"
                " Or set HERMES_OPENAI_MODEL."
            ) from exc
        models = [m.get("id") for m in body.get("data", []) if m.get("id")]
        if not models:
            raise OpenAICompatError(f"{base}/models lists no models; set HERMES_OPENAI_MODEL")
        if len(models) > 1:
            log.warning(
                "server lists several models; using the first",
                extra={"model": models[0], "available": len(models)},
            )
        _discovered[base] = models[0]
        return models[0]


def classify(message: Message, examples=None, config=None, client=None) -> Verdict:
    from .config import Config

    config = config or Config()
    examples = examples or []
    safe = redact_message(message)
    model = resolve_model(config)

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 512,
        "messages": [
            {"role": "system", "content": system_text(examples)},
            {"role": "user", "content": build_user(safe)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "schema": SCHEMA, "strict": True},
        },
    }

    try:
        body = post_json(
            _base(config) + "/chat/completions",
            payload,
            timeout=config.openai_timeout,
            retries=config.http_retries,
            backoff=config.http_backoff,
            headers=_headers(config),
        )
    except HttpError as exc:
        if exc.status == 400 and "response_format" in exc.body:
            raise OpenAICompatError(
                f"{config.openai_base_url} rejected the json_schema response_format — this"
                f" server does not support structured outputs: {exc.body[:200]}"
            ) from exc
        if exc.status is not None:
            raise OpenAICompatError(f"server returned {exc.status}: {exc.body[:200]}") from exc
        raise OpenAICompatError(
            f"cannot reach {config.openai_base_url} ({exc}) — is the server running?"
        ) from exc

    choices = body.get("choices") or []
    if not choices:
        raise OpenAICompatError(f"no choices in response: {str(body)[:200]}")
    choice = choices[0]
    content = (choice.get("message") or {}).get("content") or ""
    if choice.get("finish_reason") == "length":
        raise OpenAICompatError("verdict was cut off at max_tokens; the JSON is incomplete")

    usage = body.get("usage") or {}
    log.debug(
        "openai-compat call",
        extra={
            "model": model,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
    )

    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OpenAICompatError(f"server did not return valid JSON: {content[:200]}") from exc
    if not isinstance(data, dict):
        raise OpenAICompatError(f"expected a JSON object, got: {content[:200]}")
    try:
        return coerce_verdict(data)
    except ValueError as exc:
        raise OpenAICompatError(f"server returned an unusable verdict: {exc}") from exc


__all__ = ["OpenAICompatError", "classify", "resolve_model"]
