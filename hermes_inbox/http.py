"""Small JSON-over-HTTP client with retries.

The Anthropic SDK already retries connection errors, 408, 409, 429 and 5xx. The
urllib paths — Ollama and Telegram — did not, so a transient blip dropped a
notification or failed a classification outright.

What is retried, and what deliberately is not:

| Outcome | Retried | Why |
|---|---|---|
| Connection refused, DNS failure, timeout | yes | The server may be starting up |
| 408, 429 | yes, honouring `Retry-After` | Explicitly "try again" |
| 5xx | yes | Server-side and usually transient |
| Other 4xx | **no** | A bad token or malformed request will fail identically |

Backoff is exponential with jitter, so a restarting service does not get a
synchronized retry storm from several pollers at once.
"""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

from .logs import get_logger

log = get_logger(__name__)

RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """A request that failed after exhausting retries."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralize backoff without touching `time`."""
    time.sleep(seconds)


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    raw = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None  # HTTP-date form; fall back to computed backoff


def post_json(
    url: str,
    payload: dict,
    *,
    timeout: float = 30,
    retries: int = 3,
    backoff: float = 0.5,
    max_backoff: float = 30.0,
    headers: dict[str, str] | None = None,
) -> dict:
    """POST JSON, parse the JSON response, retry transient failures.

    `retries` is the number of *additional* attempts, so retries=3 means up to
    four requests. Raises `HttpError` when they are all exhausted.
    """
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    body = json.dumps(payload).encode("utf-8")
    last: HttpError | None = None

    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=request_headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            last = HttpError(f"HTTP {exc.code}: {detail[:200]}", status=exc.code, body=detail)
            if exc.code not in RETRY_STATUS:
                raise last from exc  # a 401 will not fix itself
            wait = _retry_after(exc)

        except urllib.error.URLError as exc:
            last = HttpError(f"{exc.reason}")
            wait = None

        except json.JSONDecodeError as exc:
            raise HttpError(f"response was not JSON: {exc}") from exc

        if attempt == retries:
            break

        delay = wait if wait is not None else min(backoff * (2**attempt), max_backoff)
        delay += random.uniform(0, delay * 0.1)  # jitter, to desynchronize pollers
        log.warning(
            "request failed, retrying",
            extra={
                "url": url.split("?")[0],
                "attempt": attempt + 1,
                "of": retries,
                "wait": round(delay, 2),
                "error": str(last),
            },
        )
        _sleep(delay)

    assert last is not None
    log.error(
        "request failed after all retries",
        extra={"url": url.split("?")[0], "attempts": retries + 1, "error": str(last)},
    )
    raise last
