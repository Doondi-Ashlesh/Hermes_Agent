"""Tests for everything that does not require a network call.

The classifier is stubbed throughout: what is under test is the redaction pass,
the deterministic gate, the correction loop, and the run loop's bookkeeping.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_inbox.agent import Agent
from hermes_inbox.config import Config, GateConfig
from hermes_inbox.feedback import Example, FeedbackStore, render_examples
from hermes_inbox.gate import decide
from hermes_inbox.redact import redact
from hermes_inbox.schema import Decision, GateDecision, Message, Verdict
from hermes_inbox.sources.fixtures import FixtureSource

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "inbox.json"


def make_message(**overrides) -> Message:
    defaults = dict(
        uid="1",
        source="test",
        sender="someone@example.com",
        subject="Hello",
        body="Body text",
        received_at=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Message(**defaults)


def make_verdict(score: float = 0.9, category: str = "personal") -> Verdict:
    return Verdict(
        important=score >= 0.5, score=score, category=category, reason="because"
    )


# --------------------------------------------------------------------------- #
# redaction
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text,expected_absent",
    [
        ("card 4111 1111 1111 1111 here", "4111 1111 1111 1111"),
        ("your code is 483920", "483920"),
        ("token ghp_9fKq2mNvR8sT4wXyZ1aB3cD5eF7gH0jK2lM4", "ghp_9fKq2mNvR8sT4wXyZ1aB3cD5eF7gH0jK2lM4"),
        ("call +1 415 555 0198 now", "+1 415 555 0198"),
    ],
)
def test_redact_removes_secrets(text, expected_absent):
    assert expected_absent not in redact(text)


def test_redact_preserves_ordinary_text():
    text = "Can we move the sync to Thursday afternoon?"
    assert redact(text) == text


def test_redact_strips_url_credentials():
    out = redact("go to https://x.example/login?token=a9f3k2m8s7d1 now")
    assert "a9f3k2m8s7d1" not in out
    assert "https://x.example/login?token=" in out


def test_redact_handles_empty():
    assert redact("") == ""


# --------------------------------------------------------------------------- #
# gate — rule precedence is the contract
# --------------------------------------------------------------------------- #


def test_never_sender_beats_high_score():
    cfg = GateConfig(threshold=0.5, never_senders=["noisy@example.com"])
    got = decide(make_message(sender="noisy@example.com"), make_verdict(0.99), cfg)
    assert got.notify is False
    assert got.rule.startswith("never-sender")


def test_always_sender_beats_low_score():
    cfg = GateConfig(threshold=0.9, always_senders=["boss@example.com"])
    got = decide(make_message(sender="boss@example.com"), make_verdict(0.01), cfg)
    assert got.notify is True


def test_never_sender_beats_always_sender():
    cfg = GateConfig(
        threshold=0.5,
        never_senders=["x@example.com"],
        always_senders=["x@example.com"],
    )
    assert decide(make_message(sender="x@example.com"), make_verdict(0.9), cfg).notify is False


def test_domain_pattern_matches_any_address():
    cfg = GateConfig(threshold=0.9, always_senders=["lumenlabs.example"])
    got = decide(make_message(sender="anyone@lumenlabs.example"), make_verdict(0.1), cfg)
    assert got.notify is True


def test_keyword_trigger_fires_on_subject():
    cfg = GateConfig(threshold=0.99, always_keywords=["invoice"])
    got = decide(make_message(subject="Your invoice is ready"), make_verdict(0.1), cfg)
    assert got.notify is True
    assert got.rule == "always-keyword:invoice"


def test_muted_category_suppresses():
    cfg = GateConfig(threshold=0.1, muted_categories=["newsletter"])
    got = decide(make_message(), make_verdict(0.95, category="newsletter"), cfg)
    assert got.notify is False


def test_threshold_boundary_is_inclusive():
    cfg = GateConfig(threshold=0.7)
    assert decide(make_message(), make_verdict(0.7), cfg).notify is True
    assert decide(make_message(), make_verdict(0.699), cfg).notify is False


def test_quiet_hours_wrapping_midnight():
    cfg = GateConfig(threshold=0.1, quiet_start=22, quiet_end=7)
    at_3am = datetime(2026, 8, 26, 3, 0).astimezone()
    assert decide(make_message(), make_verdict(0.99), cfg, now=at_3am).notify is False
    at_noon = datetime(2026, 8, 26, 12, 0).astimezone()
    assert decide(make_message(), make_verdict(0.99), cfg, now=at_noon).notify is True


def test_gate_always_records_a_rule():
    cfg = GateConfig()
    assert decide(make_message(), make_verdict(0.1), cfg).rule


# --------------------------------------------------------------------------- #
# feedback store
# --------------------------------------------------------------------------- #


def test_feedback_roundtrip(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    store.add(Example.from_message(make_message(uid="7"), True, note="a real customer"))
    store.add(Example.from_message(make_message(uid="8"), False))

    assert len(store.all()) == 2
    assert store.counts() == (1, 1)
    assert "a real customer" in render_examples(store.all())


def test_leave_one_out_excludes_target(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    for uid in ("1", "2", "3"):
        store.add(Example.from_message(make_message(uid=uid), True))

    kept = store.recent(10, exclude_uid="2")
    assert [e.uid for e in kept] == ["1", "3"]


def test_recent_returns_newest(tmp_path):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    for uid in "12345":
        store.add(Example.from_message(make_message(uid=uid), True))
    assert [e.uid for e in store.recent(2)] == ["4", "5"]


def test_store_tolerates_corrupt_line(tmp_path):
    path = tmp_path / "fb.jsonl"
    store = FeedbackStore(path)
    store.add(Example.from_message(make_message(uid="1"), True))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
    assert len(store.all()) == 1


def test_empty_store_is_empty(tmp_path):
    assert FeedbackStore(tmp_path / "missing.jsonl").all() == []


# --------------------------------------------------------------------------- #
# fixtures source
# --------------------------------------------------------------------------- #


def test_fixture_source_loads():
    messages = FixtureSource(FIXTURES).fetch_new()
    assert len(messages) == 12
    assert all(isinstance(m, Message) for m in messages)


def test_fixture_source_respects_since_uid():
    messages = FixtureSource(FIXTURES).fetch_new(since_uid="109")
    assert [m.uid for m in messages] == ["110", "111", "112"]


# --------------------------------------------------------------------------- #
# agent loop
# --------------------------------------------------------------------------- #


class StubNotifier:
    name = "stub"

    def __init__(self, labels=None):
        self.sent: list[Decision] = []
        self._labels = labels or []

    def send(self, decision):
        self.sent.append(decision)

    def poll_feedback(self, offset=None):
        labels, self._labels = self._labels, []
        return labels, (offset or 0) + len(labels)


def scoring_classifier(scores: dict[str, float]):
    def classify(message, examples, config, client=None):
        return make_verdict(scores.get(message.uid, 0.0))

    return classify


def make_agent(tmp_path, notifier, classify_fn, **gate_kwargs):
    config = Config(data_dir=tmp_path, gate=GateConfig(threshold=0.5, **gate_kwargs))
    return Agent(
        source=FixtureSource(FIXTURES),
        notifier=notifier,
        config=config,
        classify_fn=classify_fn,
    )


def test_cycle_notifies_only_above_threshold(tmp_path):
    notifier = StubNotifier()
    agent = make_agent(tmp_path, notifier, scoring_classifier({"103": 0.9, "110": 0.8}))

    result = agent.cycle()

    assert result.fetched == 12
    assert result.notified == 2
    assert {d.message.uid for d in notifier.sent} == {"103", "110"}


def test_cycle_is_idempotent(tmp_path):
    notifier = StubNotifier()
    agent = make_agent(tmp_path, notifier, scoring_classifier({"103": 0.9}))

    first = agent.cycle()
    second = agent.cycle()

    assert first.fetched == 12
    assert second.fetched == 0
    assert second.notified == 0


def test_classifier_failure_stops_the_cycle_without_losing_mail(tmp_path):
    """A provider outage must never mark unclassified mail as seen."""
    calls: list[str] = []

    def explode(message, examples, config, client=None):
        calls.append(message.uid)
        if message.uid == "103":
            raise RuntimeError("provider down")
        return make_verdict(0.9)

    agent = make_agent(tmp_path, StubNotifier(), explode)
    result = agent.cycle()

    assert any("103" in e for e in result.errors)
    assert calls == ["101", "102", "103"]  # stopped, did not grind through the rest
    assert agent.state.last_uid("fixtures") == "102"  # cursor left before the failure


def test_a_failed_message_is_retried_on_the_next_cycle(tmp_path):
    attempts: list[str] = []
    healthy = {"value": False}

    def flaky(message, examples, config, client=None):
        attempts.append(message.uid)
        if message.uid == "103" and not healthy["value"]:
            raise RuntimeError("provider down")
        return make_verdict(0.9)

    agent = make_agent(tmp_path, StubNotifier(), flaky)
    agent.cycle()
    assert "103" in attempts

    healthy["value"] = True
    attempts.clear()
    result = agent.cycle()

    assert attempts[0] == "103", "the failed message must be retried, not skipped"
    assert result.fetched == 10  # 103 onwards


def test_notifier_failure_is_recorded_not_raised(tmp_path):
    class Broken(StubNotifier):
        def send(self, decision):
            raise RuntimeError("telegram down")

    agent = make_agent(tmp_path, Broken(), scoring_classifier({"103": 0.9}))
    result = agent.cycle()

    assert result.notified == 0
    assert any("notify failed" in e for e in result.errors)


def test_button_press_becomes_a_correction(tmp_path):
    notifier = StubNotifier()
    agent = make_agent(tmp_path, notifier, scoring_classifier({"104": 0.9}))
    agent.cycle()
    assert notifier.sent[0].message.uid == "104"

    # The user says "no, marketing mail is never important".
    notifier._labels = [("104", False)]
    result = agent.cycle()

    assert result.labels_applied == 1
    stored = agent.feedback.all()
    assert len(stored) == 1
    assert stored[0].uid == "104"
    assert stored[0].label is False


def test_correction_reaches_the_next_prompt(tmp_path):
    seen: list[list[Example]] = []

    def recording(message, examples, config, client=None):
        seen.append(list(examples))
        return make_verdict(0.9 if message.uid == "104" else 0.0)

    notifier = StubNotifier()
    agent = make_agent(tmp_path, notifier, recording)
    agent.cycle()
    assert all(not examples for examples in seen)  # nothing learned yet

    notifier._labels = [("104", False)]
    agent.cycle()

    # A fresh agent over the same data dir sees the stored correction.
    seen.clear()
    agent2 = Agent(
        source=FixtureSource(FIXTURES),
        notifier=StubNotifier(),
        config=Config(data_dir=tmp_path, gate=GateConfig(threshold=0.5)),
        classify_fn=recording,
    )
    agent2.state.set_last_uid("fixtures", "0")
    agent2.cycle()
    assert seen and seen[0], "corrections should be injected into later prompts"
    assert seen[0][0].uid == "104"


def test_feedback_for_unknown_uid_is_reported(tmp_path):
    notifier = StubNotifier(labels=[("999", True)])
    agent = make_agent(tmp_path, notifier, scoring_classifier({}))
    result = agent.cycle()
    assert any("999" in e for e in result.errors)


def test_decisions_are_logged(tmp_path):
    agent = make_agent(tmp_path, StubNotifier(), scoring_classifier({"103": 0.9}))
    agent.cycle()

    lines = (tmp_path / "decisions.jsonl").read_text().strip().splitlines()
    assert len(lines) == 12
    assert agent.log.find("103").gate.notify is True


def test_run_honours_max_cycles(tmp_path):
    agent = make_agent(tmp_path, StubNotifier(), scoring_classifier({}))
    agent.run(interval=0, max_cycles=2)  # must terminate


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #


def test_decision_roundtrips_through_json():
    decision = Decision(
        message=make_message(),
        verdict=make_verdict(),
        gate=GateDecision(notify=True, rule="score>=0.7"),
    )
    restored = Decision.from_dict(json.loads(json.dumps(decision.to_dict())))
    assert restored.message.uid == decision.message.uid
    assert restored.verdict.score == decision.verdict.score
    assert restored.gate.rule == decision.gate.rule


def test_snippet_collapses_and_truncates():
    message = make_message(body="a\n\n  b   c" + "x" * 500)
    assert message.snippet(20).endswith("…")
    assert "\n" not in message.snippet()
