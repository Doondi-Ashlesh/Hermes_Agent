"""The correction store: what reaches the prompt and the eval when you change your mind."""

from __future__ import annotations

import pytest

from hermes_inbox import evals
from hermes_inbox.config import Config
from hermes_inbox.feedback import Example, FeedbackStore
from hermes_inbox.schema import Verdict


def label(uid: str, important: bool, note: str = "") -> Example:
    return Example(uid, f"{uid}@x.example", f"subject {uid}", "snip", important, note)


@pytest.fixture
def store(tmp_path):
    return FeedbackStore(tmp_path / "feedback.jsonl")


def test_a_relabel_replaces_the_earlier_label_in_the_prompt(store):
    store.add(label("1", False, "looked like a newsletter"))
    store.add(label("2", True))
    store.add(label("1", True, "it was the client"))

    recent = store.recent(40)
    assert [(e.uid, e.label) for e in recent] == [("2", True), ("1", True)]
    assert "it was the client" in recent[-1].note


def test_a_relabel_counts_as_the_newest_correction(store):
    """Order is recency of the decision, so a relabel survives HERMES_MAX_EXAMPLES."""
    store.add(label("1", False))
    for uid in "234":
        store.add(label(uid, True))
    store.add(label("1", True))
    assert [e.uid for e in store.recent(2)] == ["4", "1"]


def test_counts_are_per_message_not_per_button_press(store):
    store.add(label("1", False))
    store.add(label("1", True))
    store.add(label("2", False))
    assert store.counts() == (1, 1)


def test_history_is_kept(store):
    """Append-only: nothing is rewritten, so the change of mind stays auditable."""
    store.add(label("1", False))
    store.add(label("1", True))
    assert [e.label for e in store.all()] == [False, True]
    assert len(store.path.read_text(encoding="utf-8").splitlines()) == 2


def test_the_eval_scores_a_relabeled_message_once_against_its_latest_label(store):
    store.add(label("1", False))
    store.add(label("2", False))
    store.add(label("1", True))

    def oracle(message, examples, config, client=None):
        return Verdict(True, 1.0 if message.uid == "1" else 0.0, "other", "r")

    report = evals.run_eval(store, Config(), classify_fn=oracle)
    assert report.total == 2
    assert report.accuracy == 1.0


def test_leave_one_out_drops_every_label_for_the_message_being_scored(store):
    store.add(label("1", False))
    store.add(label("2", True))
    store.add(label("1", True))

    seen = {}

    def spy(message, examples, config, client=None):
        seen[message.uid] = [e.uid for e in examples]
        return Verdict(False, 0.0, "other", "r")

    evals.run_eval(store, Config(), classify_fn=spy)
    assert seen == {"1": ["2"], "2": ["1"]}


# --------------------------------------------------------------------------- #
# headers (F-015): the eval must replay what the live loop saw
# --------------------------------------------------------------------------- #


def bulk_message(uid: str = "7"):
    from datetime import datetime, timezone

    from hermes_inbox.schema import Message

    return Message(
        uid=uid,
        source="imap",
        sender="news@vendor.example",
        subject="Your invoice is ready",
        body="Your monthly invoice is attached.",
        received_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        headers={"List-Unsubscribe": "<https://vendor.example/u>", "Precedence": "bulk"},
    )


def test_a_correction_keeps_the_messages_headers(store):
    store.add(Example.from_message(bulk_message(), label=False))
    assert store.current()[0].headers == {
        "List-Unsubscribe": "<https://vendor.example/u>",
        "Precedence": "bulk",
    }


def test_corrections_written_before_headers_existed_still_load(store):
    store.path.write_text(
        '{"uid": "1", "sender": "a@x", "subject": "s", "snippet": "b", "label": true,'
        ' "note": "", "labeled_at": ""}\n',
        encoding="utf-8",
    )
    assert [(e.uid, e.headers) for e in store.current()] == [("1", {})]


def test_eval_replays_a_correction_exactly_as_the_live_loop_scored_it(store):
    """The offline rules weight bulk mail down by header. Without the headers the
    replayed message scores differently from the one you actually corrected."""
    from hermes_inbox.offline import classify as offline_classify

    live = bulk_message()
    store.add(Example.from_message(live, label=False))

    replayed = evals.cases_from_store(store)[0].message
    assert replayed.headers == live.headers
    assert offline_classify(replayed).score == offline_classify(live).score


def test_headers_do_not_change_the_prompt(store):
    """Stored for replay, not rendered: the correction block in the prompt is unchanged."""
    example = Example.from_message(bulk_message(), label=False)
    assert "List-Unsubscribe" not in example.render()


def test_older_corrections_recover_their_headers_from_the_decision_log(store, tmp_path):
    """A correction made before headers were stored still replays faithfully,
    as long as the decision it corrected is in the log."""
    from hermes_inbox.schema import Decision, GateDecision, Verdict
    from hermes_inbox.state import DecisionLog

    live = bulk_message()
    log = DecisionLog(tmp_path / "decisions.jsonl")
    log.append(
        Decision(live, Verdict(False, 0.6, "billing", "r"), GateDecision(False, "score<0.7"))
    )
    legacy = Example.from_message(live, label=False)
    store.add(Example(**{**legacy.__dict__, "headers": {}}))  # as written before F-015

    assert evals.cases_from_store(store)[0].message.headers == {}
    assert evals.cases_from_store(store, log)[0].message.headers == live.headers


def test_a_correction_from_a_newer_version_is_read_not_dropped(store):
    store.path.write_text(
        '{"uid": "1", "sender": "a@x", "subject": "s", "snippet": "b", "label": true,'
        ' "note": "", "labeled_at": "", "headers": {}, "added_in_a_later_version": 3}\n',
        encoding="utf-8",
    )
    assert [e.uid for e in store.current()] == ["1"]
