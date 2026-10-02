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
