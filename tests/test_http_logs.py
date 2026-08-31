"""Retry policy and logging setup.

The retry tests are about *policy* — which failures are worth trying again and
which are not. Retrying a 401 forever is as much a bug as not retrying a 503.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request

import pytest

from hermes_inbox import http, logs


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    monkeypatch.setattr("hermes_inbox.http._sleep", lambda seconds: None)


def responder(*outcomes):
    """Return a urlopen stub that yields each outcome in turn.

    An outcome is either a dict (success) or an exception to raise.
    """
    calls = {"n": 0}

    def _open(request, timeout=None):
        outcome = outcomes[min(calls["n"], len(outcomes) - 1)]
        calls["n"] += 1
        if isinstance(outcome, BaseException):
            raise outcome

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(json.dumps(outcome).encode())

    _open.calls = calls
    return _open


def http_error(code, headers=None):
    return urllib.error.HTTPError(
        "http://x.example", code, "err", headers or {}, io.BytesIO(b"detail")
    )


# --------------------------------------------------------------------------- #
# what gets retried
# --------------------------------------------------------------------------- #


def test_succeeds_first_time_without_retrying(monkeypatch):
    stub = responder({"ok": True})
    monkeypatch.setattr(urllib.request, "urlopen", stub)

    assert http.post_json("http://x.example", {}) == {"ok": True}
    assert stub.calls["n"] == 1


def test_recovers_after_a_transient_connection_failure(monkeypatch):
    stub = responder(urllib.error.URLError("Connection refused"), {"ok": True})
    monkeypatch.setattr(urllib.request, "urlopen", stub)

    assert http.post_json("http://x.example", {}) == {"ok": True}
    assert stub.calls["n"] == 2


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
def test_transient_status_codes_are_retried(monkeypatch, code):
    stub = responder(http_error(code), {"ok": True})
    monkeypatch.setattr(urllib.request, "urlopen", stub)

    assert http.post_json("http://x.example", {})["ok"] is True
    assert stub.calls["n"] == 2


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_client_errors_fail_immediately(monkeypatch, code):
    """A bad token will fail identically every time — retrying wastes the cycle."""
    stub = responder(http_error(code))
    monkeypatch.setattr(urllib.request, "urlopen", stub)

    with pytest.raises(http.HttpError) as exc:
        http.post_json("http://x.example", {}, retries=5)
    assert exc.value.status == code
    assert stub.calls["n"] == 1, "a 4xx must not be retried"


def test_gives_up_after_the_configured_attempts(monkeypatch):
    stub = responder(urllib.error.URLError("down"))
    monkeypatch.setattr(urllib.request, "urlopen", stub)

    with pytest.raises(http.HttpError, match="down"):
        http.post_json("http://x.example", {}, retries=2)
    assert stub.calls["n"] == 3, "retries=2 means three attempts total"


def test_retries_can_be_disabled(monkeypatch):
    stub = responder(urllib.error.URLError("down"))
    monkeypatch.setattr(urllib.request, "urlopen", stub)

    with pytest.raises(http.HttpError):
        http.post_json("http://x.example", {}, retries=0)
    assert stub.calls["n"] == 1


def test_retry_after_header_is_honoured(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr("hermes_inbox.http._sleep", waits.append)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        responder(http_error(429, {"Retry-After": "7"}), {"ok": True}),
    )

    http.post_json("http://x.example", {}, backoff=0.5)
    assert 7 <= waits[0] <= 7.7, f"server asked for 7s, waited {waits[0]}"


def test_backoff_grows_and_is_jittered(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr("hermes_inbox.http._sleep", waits.append)
    monkeypatch.setattr(urllib.request, "urlopen", responder(urllib.error.URLError("down")))

    with pytest.raises(http.HttpError):
        http.post_json("http://x.example", {}, retries=3, backoff=1.0)

    assert len(waits) == 3
    assert waits[0] < waits[1] < waits[2], f"not exponential: {waits}"
    assert all(base <= w <= base * 1.1 for w, base in zip(waits, [1.0, 2.0, 4.0]))


def test_backoff_is_capped(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr("hermes_inbox.http._sleep", waits.append)
    monkeypatch.setattr(urllib.request, "urlopen", responder(urllib.error.URLError("down")))

    with pytest.raises(http.HttpError):
        http.post_json("http://x.example", {}, retries=6, backoff=10, max_backoff=15)
    assert max(waits) <= 15 * 1.1


def test_non_json_response_is_not_retried(monkeypatch):
    def garbage(request, timeout=None):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(b"<html>oops</html>")

    monkeypatch.setattr(urllib.request, "urlopen", garbage)
    with pytest.raises(http.HttpError, match="not JSON"):
        http.post_json("http://x.example", {})


def test_url_query_is_not_logged(monkeypatch, caplog):
    """Telegram puts the bot token in the path; keep it out of the logs."""
    monkeypatch.setattr(urllib.request, "urlopen", responder(urllib.error.URLError("down")))
    logs.configure("DEBUG")

    with caplog.at_level(logging.WARNING, logger="hermes_inbox"):
        with pytest.raises(http.HttpError):
            http.post_json("http://x.example/path?secret=abc123", {}, retries=1)

    assert "abc123" not in caplog.text


# --------------------------------------------------------------------------- #
# logging setup
# --------------------------------------------------------------------------- #


def test_text_format_includes_level_and_extras():
    stream = io.StringIO()
    logs.configure("INFO", "text", stream=stream)
    logs.get_logger("t").info("notified", extra={"uid": "42", "score": 0.9})

    out = stream.getvalue()
    assert "INFO" in out and "notified" in out
    assert "uid=42" in out and "score=0.9" in out


def test_json_format_is_one_parseable_object_per_line():
    stream = io.StringIO()
    logs.configure("INFO", "json", stream=stream)
    logs.get_logger("t").info("decided", extra={"uid": "7", "notify": True})

    payload = json.loads(stream.getvalue().strip())
    assert payload["msg"] == "decided"
    assert payload["level"] == "INFO"
    assert payload["uid"] == "7"
    assert payload["notify"] is True
    assert "ts" in payload


def test_level_filters_output():
    stream = io.StringIO()
    logs.configure("WARNING", "text", stream=stream)
    log = logs.get_logger("t")
    log.debug("invisible")
    log.info("also invisible")
    log.warning("visible")

    out = stream.getvalue()
    assert "invisible" not in out
    assert "visible" in out


def test_configure_is_idempotent():
    """Calling it twice must not double every line."""
    stream = io.StringIO()
    logs.configure("INFO", "text", stream=stream)
    logs.configure("INFO", "text", stream=stream)
    logs.get_logger("t").info("once")

    assert stream.getvalue().count("once") == 1


def test_does_not_hijack_the_root_logger():
    """Importing this package must not change a host application's logging."""
    logs.configure("INFO", "text", stream=io.StringIO())
    assert logging.getLogger(logs.LOGGER_NAME).propagate is False


def test_exceptions_are_rendered():
    stream = io.StringIO()
    logs.configure("ERROR", "json", stream=stream)
    try:
        raise ValueError("boom")
    except ValueError:
        logs.get_logger("t").error("failed", exc_info=True)

    payload = json.loads(stream.getvalue().strip())
    assert "ValueError: boom" in payload["exc"]
