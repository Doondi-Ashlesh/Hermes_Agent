"""Provider registry and the Ollama adapter.

The Ollama tests stub urlopen — no local server needed.
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from hermes_inbox import ollama, openai_compat, providers
from hermes_inbox.config import Config
from hermes_inbox.feedback import Example
from hermes_inbox.schema import Message, Verdict


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Retries are real; the waiting between them must not be, in tests."""
    monkeypatch.setattr("hermes_inbox.http._sleep", lambda seconds: None)


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

    fn, name = providers.resolve("openai-compat")
    assert name == "openai-compat" and callable(fn)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        providers.resolve("gpt-9")


def test_describe_names_what_will_run():
    config = Config(model="claude-sonnet-5", ollama_model="qwen2.5:7b")
    assert "claude-sonnet-5" in providers.describe("anthropic", config)
    assert "qwen2.5:7b" in providers.describe("ollama", config)
    assert "does not learn" in providers.describe("offline", config)
    assert "localhost:8000" in providers.describe("openai-compat", config)
    assert "my-model" in providers.describe("openai-compat", Config(openai_model="my-model"))


@pytest.mark.parametrize("name", ["anthropic", "ollama", "openai-compat", "offline"])
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


# --------------------------------------------------------------------------- #
# openai-compatible adapter (vLLM, NIM)
# --------------------------------------------------------------------------- #


class FakeServer:
    """Stands in for `/v1/models` and `/v1/chat/completions`, and records requests."""

    def __init__(self, verdict=GOOD, models=("served/model",), finish_reason="stop", content=None):
        self.verdict = verdict
        self.models = list(models)
        self.finish_reason = finish_reason
        self.content = content
        self.requests: list[tuple[str, dict | None, dict]] = []

    def __call__(self, request, timeout=None):
        body = json.loads(request.data.decode()) if request.data else None
        self.requests.append((request.full_url, body, dict(request.header_items())))
        if request.full_url.endswith("/models"):
            reply = {"object": "list", "data": [{"id": m} for m in self.models]}
        else:
            content = self.content if self.content is not None else json.dumps(self.verdict)
            reply = {
                "choices": [
                    {"message": {"role": "assistant", "content": content},
                     "finish_reason": self.finish_reason}
                ],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40},
            }

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(json.dumps(reply).encode())

    def chat(self) -> dict:
        return next(body for url, body, _ in self.requests if url.endswith("/chat/completions"))


@pytest.fixture(autouse=True)
def forget_discovered_models():
    openai_compat._discovered.clear()
    yield
    openai_compat._discovered.clear()


def test_openai_compat_parses_a_verdict(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", FakeServer())
    verdict = openai_compat.classify(make_message(), [], Config(openai_model="m"))
    assert verdict.score == 0.81 and verdict.category == "lead"


def test_openai_compat_constrains_output_and_redacts(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", server)
    openai_compat.classify(make_message(), [], Config(openai_model="m"))

    sent = server.chat()
    assert sent["temperature"] == 0
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert "4111 1111 1111 1111" not in json.dumps(sent)


def test_openai_compat_carries_corrections_in_the_system_turn(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", server)
    examples = [Example("9", "d@s.example", "sale", "snip", False, "never ping me for sales")]
    openai_compat.classify(make_message(), examples, Config(openai_model="m"))
    assert "never ping me for sales" in server.chat()["messages"][0]["content"]


def test_openai_compat_discovers_the_served_model_once(monkeypatch):
    server = FakeServer(models=("nvidia/llama-3.1-nemotron-nano-8b-v1",))
    monkeypatch.setattr(urllib.request, "urlopen", server)
    config = Config(openai_model="")
    for _ in range(3):
        openai_compat.classify(make_message(), [], config)

    listed = [url for url, _, _ in server.requests if url.endswith("/models")]
    assert len(listed) == 1
    assert server.chat()["model"] == "nvidia/llama-3.1-nemotron-nano-8b-v1"


def test_openai_compat_uses_the_configured_model_without_asking(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", server)
    openai_compat.classify(make_message(), [], Config(openai_model="pinned"))
    assert not any(url.endswith("/models") for url, _, _ in server.requests)
    assert server.chat()["model"] == "pinned"


def test_openai_compat_sends_a_key_only_when_one_is_set(monkeypatch):
    server = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", server)
    openai_compat.classify(make_message(), [], Config(openai_model="m"))
    openai_compat.classify(make_message(), [], Config(openai_model="m", openai_api_key="k-123"))
    auth = [headers.get("Authorization") for _, _, headers in server.requests]
    assert auth == [None, "Bearer k-123"]


@pytest.mark.parametrize(
    "raw,expected_score,expected_category",
    [
        ({**GOOD, "score": 7.0}, 1.0, "lead"),
        ({**GOOD, "category": "invented"}, 0.81, "other"),
    ],
)
def test_openai_compat_clamps_sloppy_output(monkeypatch, raw, expected_score, expected_category):
    monkeypatch.setattr(urllib.request, "urlopen", FakeServer(verdict=raw))
    verdict = openai_compat.classify(make_message(), [], Config(openai_model="m"))
    assert (verdict.score, verdict.category) == (expected_score, expected_category)


def test_openai_compat_refuses_a_truncated_verdict(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", FakeServer(finish_reason="length"))
    with pytest.raises(openai_compat.OpenAICompatError, match="max_tokens"):
        openai_compat.classify(make_message(), [], Config(openai_model="m"))


def test_openai_compat_reports_unparseable_output(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", FakeServer(content="Sure! Here you go:"))
    with pytest.raises(openai_compat.OpenAICompatError, match="valid JSON"):
        openai_compat.classify(make_message(), [], Config(openai_model="m"))


def test_openai_compat_reports_a_missing_server_clearly(monkeypatch):
    def refuse(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(openai_compat.OpenAICompatError, match="is the server running"):
        openai_compat.classify(make_message(), [], Config(openai_model="m"))
    with pytest.raises(openai_compat.OpenAICompatError, match="HERMES_OPENAI_MODEL"):
        openai_compat.classify(make_message(), [], Config(openai_model=""))


def test_openai_compat_explains_a_server_without_structured_outputs(monkeypatch):
    def reject(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error": "response_format json_schema is not supported"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    with pytest.raises(openai_compat.OpenAICompatError, match="structured outputs"):
        openai_compat.classify(make_message(), [], Config(openai_model="m"))


def test_concurrent_eval_discovers_the_model_once(monkeypatch, tmp_path, capsys):
    """The whole path: CLI, parallel replay, discovery under a lock, real request shape."""
    from hermes_inbox.cli import main

    server = FakeServer()
    monkeypatch.setattr(urllib.request, "urlopen", server)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_OPENAI_BASE_URL", "http://gpu-box:8000/v1")
    monkeypatch.delenv("HERMES_OPENAI_MODEL", raising=False)

    assert main(["eval", "--golden", "--provider", "openai-compat", "--concurrency", "4", "--json"]) == 0
    assert sum(url.endswith("/models") for url, _, _ in server.requests) == 1
    assert sum(url.endswith("/chat/completions") for url, _, _ in server.requests) == 12
    assert json.loads(capsys.readouterr().out)["openai-compat"]["total"] == 12


@pytest.mark.parametrize("score", ["high", None, float("nan")])
@pytest.mark.parametrize("module,error", [(ollama, "OllamaError"), (openai_compat, "OpenAICompatError")])
def test_a_verdict_without_a_usable_score_fails_loudly(monkeypatch, module, error, score):
    """No score is not a low score. Defaulting to 0 would silently drop the message (F-004)."""
    raw = {**GOOD, "score": score}
    if module is ollama:
        monkeypatch.setattr(urllib.request, "urlopen", stub_urlopen(raw))
    else:
        monkeypatch.setattr(urllib.request, "urlopen", FakeServer(verdict=raw))
    with pytest.raises(getattr(module, error), match="unusable verdict"):
        module.classify(make_message(), [], Config(openai_model="m"))


def test_an_unusable_score_stops_the_cycle_without_advancing_the_cursor(tmp_path):
    """The loop-level consequence: the message is retried next cycle, not lost."""
    from hermes_inbox.agent import Agent
    from hermes_inbox.cli import FIXTURES
    from hermes_inbox.notify.console import ConsoleNotifier
    from hermes_inbox.sources.fixtures import FixtureSource

    def garbage(message, examples, config, client=None):
        raise ollama.OllamaError("ollama returned an unusable verdict: 'high'")

    agent = Agent(FixtureSource(FIXTURES), ConsoleNotifier(), Config(data_dir=tmp_path), classify_fn=garbage)
    result = agent.cycle()
    assert result.errors and result.notified == 0
    assert agent.state.last_uid("fixtures") is None
