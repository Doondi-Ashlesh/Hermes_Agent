"""Provider registry and the Ollama adapter.

The Ollama tests stub urlopen — no local server needed.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from hermes_inbox import ollama, providers
from hermes_inbox.config import Config
from hermes_inbox.feedback import Example
from hermes_inbox.schema import Message, Verdict


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: False)


def make_message(**overrides) -> Message:
    defaults = dict(
        uid="1",
        source="test",
        sender="a@b.example",
        subject="Re: pricing",
        body="Can you send pricing? Card 4111 1111 1111 1111.",
        received_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Message(**defaults)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #


def test_auto_falls_back_to_offline_without_a_credential():
    _, name = providers.resolve("auto")
    assert name == "offline"


def test_auto_prefers_anthropic_when_a_credential_exists(monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    fn, name = providers.resolve("auto")
    assert name == "anthropic"
    assert fn is None  # None → Agent uses its default classifier


def test_explicit_provider_overrides_auto_detection():
    fn, name = providers.resolve("offline")
    assert name == "offline" and callable(fn)

    fn, name = providers.resolve("ollama")
    assert name == "ollama" and callable(fn)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        providers.resolve("gpt-9")


def test_describe_names_what_will_run():
    config = Config(model="claude-sonnet-5", ollama_model="qwen2.5:7b")
    assert "claude-sonnet-5" in providers.describe("anthropic", config)
    assert "qwen2.5:7b" in providers.describe("ollama", config)
    assert "does not learn" in providers.describe("offline", config)


@pytest.mark.parametrize("name", ["anthropic", "ollama", "offline"])
def test_every_provider_shares_the_classifier_signature(name):
    import inspect

    from hermes_inbox.classify import classify as reference

    fn, _ = providers.resolve(name)
    if fn is None:
        fn = reference
    assert list(inspect.signature(fn).parameters) == list(inspect.signature(reference).parameters)


# --------------------------------------------------------------------------- #
# ollama adapter
# --------------------------------------------------------------------------- #


def stub_urlopen(payload, capture=None):
    def _open(request, timeout=None):
        if capture is not None:
            capture.append(json.loads(request.data.decode()))
        body = json.dumps({"message": {"content": json.dumps(payload)}}).encode()

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(body)

    return _open


GOOD = {
    "important": True,
    "score": 0.81,
    "category": "lead",
    "reason": "Someone is waiting on pricing.",
    "suggested_action": "reply with pricing",
}


def test_ollama_parses_a_verdict(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", stub_urlopen(GOOD))
    verdict = ollama.classify(make_message(), [], Config())
    assert isinstance(verdict, Verdict)
    assert verdict.score == 0.81
    assert verdict.category == "lead"


def test_ollama_redacts_and_sends_the_schema(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(urllib.request, "urlopen", stub_urlopen(GOOD, captured))
    ollama.classify(make_message(), [], Config(ollama_model="llama3.1:8b"))

    sent = captured[0]
    assert sent["model"] == "llama3.1:8b"
    assert sent["stream"] is False
    assert sent["format"]["type"] == "object"
    assert sent["options"]["temperature"] == 0
    assert "4111 1111 1111 1111" not in json.dumps(sent)


def test_ollama_carries_corrections_in_its_system_turn(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(urllib.request, "urlopen", stub_urlopen(GOOD, captured))
    examples = [Example("9", "d@s.example", "sale", "snip", False, "never ping me for sales")]
    ollama.classify(make_message(), examples, Config())

    system = captured[0]["messages"][0]["content"]
    assert "never ping me for sales" in system


@pytest.mark.parametrize(
    "raw,expected_score,expected_category",
    [
        ({**GOOD, "score": 7.0}, 1.0, "lead"),          # out of range
        ({**GOOD, "score": -2}, 0.0, "lead"),           # negative
        ({**GOOD, "category": "invented"}, 0.81, "other"),  # not in the enum
    ],
)
def test_ollama_clamps_sloppy_small_model_output(monkeypatch, raw, expected_score, expected_category):
    """A weak model must not be able to crash the polling loop."""
    monkeypatch.setattr(urllib.request, "urlopen", stub_urlopen(raw))
    verdict = ollama.classify(make_message(), [], Config())
    assert verdict.score == expected_score
    assert verdict.category == expected_category


def test_ollama_reports_a_missing_server_clearly(monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(ollama.OllamaError, match="ollama serve"):
        ollama.classify(make_message(), [], Config())


def test_ollama_reports_unparseable_output(monkeypatch):
    def garbage(request, timeout=None):
        body = json.dumps({"message": {"content": "Sure! Here you go:"}}).encode()

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(body)

    monkeypatch.setattr(urllib.request, "urlopen", garbage)
    with pytest.raises(ollama.OllamaError, match="valid JSON"):
        ollama.classify(make_message(), [], Config())
