"""Classifier tests.

No network: a stub client captures the request and returns a canned response, so
these assert the *shape* of what we send and how we handle what comes back.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from hermes_inbox.classify import SCHEMA, build_prompt, classify
from hermes_inbox.config import Config
from hermes_inbox.evals import Report, run_eval
from hermes_inbox.feedback import Example, FeedbackStore
from hermes_inbox.schema import Message, Verdict


class Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class Response:
    def __init__(self, payload, stop_reason="end_turn"):
        self.content = [Block(json.dumps(payload))]
        self.stop_reason = stop_reason


class StubClient:
    """Captures kwargs; returns whatever payload it was given."""

    def __init__(self, payload=None, stop_reason="end_turn"):
        self.payload = payload or {
            "important": True,
            "score": 0.82,
            "category": "lead",
            "reason": "A named person is waiting on pricing.",
            "suggested_action": "send the 50-seat pricing",
        }
        self.stop_reason = stop_reason
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return Response(self.payload, self.stop_reason)


def make_message(**overrides) -> Message:
    defaults = dict(
        uid="1",
        source="test",
        sender="priya@northwind.example",
        subject="Re: pricing",
        body="Can you send pricing? My card is 4111 1111 1111 1111.",
        received_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Message(**defaults)


def test_classify_returns_parsed_verdict():
    verdict = classify(make_message(), [], Config(), client=StubClient())
    assert isinstance(verdict, Verdict)
    assert verdict.score == 0.82
    assert verdict.category == "lead"


def test_request_uses_the_configured_model_and_schema():
    client = StubClient()
    classify(make_message(), [], Config(model="claude-opus-5"), client=client)

    kwargs = client.calls[0]
    assert kwargs["model"] == "claude-opus-5"
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["format"]["schema"] == SCHEMA
    assert "effort" not in kwargs["output_config"]


def test_effort_is_sent_only_when_configured():
    client = StubClient()
    classify(make_message(), [], Config(effort="low"), client=client)
    assert client.calls[0]["output_config"]["effort"] == "low"


def test_system_prompt_is_cached():
    client = StubClient()
    classify(make_message(), [], Config(), client=client)
    system = client.calls[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_body_is_redacted_before_it_reaches_the_model():
    client = StubClient()
    classify(make_message(), [], Config(), client=client)
    sent = client.calls[0]["messages"][0]["content"]
    assert "4111 1111 1111 1111" not in sent
    assert "[card]" in sent


def test_corrections_are_included_in_the_prompt():
    client = StubClient()
    examples = [
        Example(
            uid="9",
            sender="deals@shop.example",
            subject="70% off",
            snippet="Big sale",
            label=False,
            note="marketing is never urgent",
        )
    ]
    classify(make_message(), examples, Config(), client=client)

    sent = client.calls[0]["messages"][0]["content"]
    assert "marketing is never urgent" in sent
    assert "NOT IMPORTANT" in sent


def test_refusal_is_surfaced_not_swallowed():
    client = StubClient(stop_reason="refusal")
    verdict = classify(make_message(), [], Config(), client=client)
    assert verdict.score == 0.0
    assert "manual" in verdict.suggested_action


def test_prompt_omits_example_section_when_there_are_none():
    prompt = build_prompt(make_message(), [])
    assert "previously corrected" not in prompt
    assert "Classify this message:" in prompt


def test_schema_is_closed():
    assert SCHEMA["additionalProperties"] is False
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


# --------------------------------------------------------------------------- #
# eval harness
# --------------------------------------------------------------------------- #


def test_eval_reports_perfect_scores_for_a_perfect_classifier(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    store.add(Example("1", "a@x.example", "urgent", "s", True))
    store.add(Example("2", "b@x.example", "sale", "s", False))

    def oracle(message, examples, config, client=None):
        return Verdict(True, 1.0 if message.uid == "1" else 0.0, "other", "r")

    report = run_eval(store, Config(), classify_fn=oracle)
    assert report.total == 2
    assert report.accuracy == 1.0
    assert report.recall == 1.0
    assert report.misses == []


def test_eval_counts_a_missed_important_mail(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    store.add(Example("1", "a@x.example", "urgent", "s", True))

    def always_quiet(message, examples, config, client=None):
        return Verdict(False, 0.0, "other", "r")

    report = run_eval(store, Config(), classify_fn=always_quiet)
    assert report.false_negative == 1
    assert report.recall == 0.0
    assert report.misses[0][2] is True


def test_eval_is_leave_one_out(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    for uid in ("1", "2", "3"):
        store.add(Example(uid, "a@x.example", "s", "s", True))

    seen: list[set[str]] = []

    def spy(message, examples, config, client=None):
        seen.append({e.uid for e in examples})
        return Verdict(True, 1.0, "other", "r")

    run_eval(store, Config(), classify_fn=spy)
    assert seen == [{"2", "3"}, {"1", "3"}, {"1", "2"}]


def test_empty_report_renders_guidance():
    assert "No labeled examples" in Report().render()


@pytest.mark.parametrize("tp,fp,fn,precision,recall", [(3, 1, 1, 0.75, 0.75), (0, 0, 0, 0.0, 0.0)])
def test_report_metrics(tp, fp, fn, precision, recall):
    report = Report(total=tp + fp + fn, true_positive=tp, false_positive=fp, false_negative=fn)
    assert report.precision == pytest.approx(precision)
    assert report.recall == pytest.approx(recall)
