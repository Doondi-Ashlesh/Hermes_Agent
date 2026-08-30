"""Durability and lookup behaviour of the run state.

These cover three defects rather than three features — see F-009, F-010, F-011
in docs/DECISIONS.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_inbox.agent import Agent
from hermes_inbox.config import Config, GateConfig
from hermes_inbox.schema import Decision, GateDecision, Message, Verdict
from hermes_inbox.sources.fixtures import FixtureSource
from hermes_inbox.state import DecisionLog, State

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "inbox.json"


def make_decision(uid: str, notify: bool = True) -> Decision:
    return Decision(
        message=Message(
            uid=uid,
            source="test",
            sender="a@b.example",
            subject=f"Subject {uid}",
            body="Body text",
            received_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
        ),
        verdict=Verdict(notify, 0.8, "personal", "reason"),
        gate=GateDecision(notify, "score>=0.7"),
    )


# --------------------------------------------------------------------------- #
# State — atomic writes
# --------------------------------------------------------------------------- #


def test_state_roundtrips(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_last_uid("imap", "42")
    state.telegram_offset = 7
    state.save()

    reloaded = State(tmp_path / "state.json")
    assert reloaded.last_uid("imap") == "42"
    assert reloaded.telegram_offset == 7


def test_save_leaves_no_temp_files(tmp_path):
    state = State(tmp_path / "state.json")
    state.set_last_uid("imap", "1")
    state.save()
    state.save()
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_corrupt_state_reads_as_empty_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"last_uid": {"imap": "4', encoding="utf-8")
    assert State(path).last_uid("imap") is None


def test_save_never_leaves_a_truncated_file(tmp_path, monkeypatch):
    """A crash mid-write must not destroy the existing cursor."""
    path = tmp_path / "state.json"
    good = State(path)
    good.set_last_uid("imap", "100")
    good.save()

    broken = State(path)
    broken.set_last_uid("imap", "200")

    real_replace = __import__("os").replace

    def explode(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("hermes_inbox.state.os.replace", explode)
    with pytest.raises(OSError):
        broken.save()
    monkeypatch.setattr("hermes_inbox.state.os.replace", real_replace)

    # The old value survived, and no debris was left behind.
    assert State(path).last_uid("imap") == "100"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# --------------------------------------------------------------------------- #
# DecisionLog — indexed lookup
# --------------------------------------------------------------------------- #


def test_find_returns_the_right_decision(tmp_path):
    log = DecisionLog(tmp_path / "d.jsonl")
    for uid in ("100", "101", "102"):
        log.append(make_decision(uid))

    assert log.find("101").message.subject == "Subject 101"
    assert log.find("nope") is None


def test_find_returns_the_most_recent_entry_for_a_uid(tmp_path):
    log = DecisionLog(tmp_path / "d.jsonl")
    log.append(make_decision("7", notify=True))
    log.append(make_decision("7", notify=False))
    assert log.find("7").gate.notify is False


def test_index_survives_appends_after_a_lookup(tmp_path):
    log = DecisionLog(tmp_path / "d.jsonl")
    log.append(make_decision("1"))
    assert log.find("1") is not None      # builds the index
    log.append(make_decision("2"))        # must update it
    assert log.find("2") is not None


def test_finds_entries_written_by_another_process(tmp_path):
    """A second writer's appends must be visible, not masked by a stale index."""
    reader = DecisionLog(tmp_path / "d.jsonl")
    writer = DecisionLog(tmp_path / "d.jsonl")

    writer.append(make_decision("1"))
    assert reader.find("1") is not None   # reader builds its index here

    writer.append(make_decision("2"))     # reader knows nothing about this
    assert reader.find("2") is not None, "stale index must be rebuilt on a miss"


def test_stale_offset_is_detected_and_rebuilt(tmp_path):
    log = DecisionLog(tmp_path / "d.jsonl")
    log.append(make_decision("1"))
    log.append(make_decision("2"))
    log.find("1")

    log._index["1"] = log._index["2"]     # corrupt: points at the wrong record
    assert log.find("1").message.uid == "1"


def test_log_tolerates_a_corrupt_line(tmp_path):
    path = tmp_path / "d.jsonl"
    log = DecisionLog(path)
    log.append(make_decision("1"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    log.append(make_decision("2"))

    assert len(log.all()) == 2
    assert log.find("2") is not None


def test_iter_all_streams_without_materializing(tmp_path):
    log = DecisionLog(tmp_path / "d.jsonl")
    for uid in range(5):
        log.append(make_decision(str(uid)))
    stream = log.iter_all()
    assert next(stream).message.uid == "0"   # yields before consuming the file


def test_empty_log_is_safe(tmp_path):
    log = DecisionLog(tmp_path / "missing.jsonl")
    assert log.all() == []
    assert log.find("1") is None


def test_lookup_does_not_scale_with_log_size(tmp_path):
    """Regression guard for F-010: find() used to parse the whole file."""
    import time

    log = DecisionLog(tmp_path / "d.jsonl")
    for uid in range(3000):
        log.append(make_decision(str(uid)))

    log.find("0")  # build the index once
    start = time.monotonic()
    for uid in ("0", "1500", "2999"):
        log.find(uid)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"3 lookups over 3000 entries took {elapsed:.2f}s — index not working"


# --------------------------------------------------------------------------- #
# the cursor is durable mid-cycle
# --------------------------------------------------------------------------- #


class StubNotifier:
    name = "stub"

    def __init__(self):
        self.sent = []

    def send(self, decision):
        self.sent.append(decision)

    def poll_feedback(self, offset=None):
        return [], offset


def test_cursor_is_persisted_per_message_not_per_cycle(tmp_path):
    """Regression guard for F-009: a crash mid-cycle used to replay everything."""
    notifier = StubNotifier()

    def crash_on_the_fifth(message, examples, config, client=None):
        if message.uid == "105":
            raise KeyboardInterrupt("simulated SIGTERM")
        return Verdict(True, 0.9, "personal", "r")

    agent = Agent(
        source=FixtureSource(FIXTURES),
        notifier=notifier,
        config=Config(data_dir=tmp_path, gate=GateConfig(threshold=0.5)),
        classify_fn=crash_on_the_fifth,
    )
    with pytest.raises(KeyboardInterrupt):
        agent.cycle()

    # Four messages were notified; the cursor on disk reflects exactly those.
    assert len(notifier.sent) == 4
    assert State(tmp_path / "state.json").last_uid("fixtures") == "104"

    # A fresh process resumes at 105 rather than re-notifying 101-104.
    resumed = StubNotifier()
    Agent(
        source=FixtureSource(FIXTURES),
        notifier=resumed,
        config=Config(data_dir=tmp_path, gate=GateConfig(threshold=0.5)),
        classify_fn=lambda m, e, c, client=None: Verdict(True, 0.9, "personal", "r"),
    ).cycle()
    assert [d.message.uid for d in resumed.sent][0] == "105"
    assert "101" not in [d.message.uid for d in resumed.sent]


def test_notifier_offset_is_saved_before_messages_are_processed(tmp_path):
    """Consumed getUpdates cannot be re-fetched, so the offset must persist early."""

    class OffsetNotifier(StubNotifier):
        def poll_feedback(self, offset=None):
            return [], 99

    def explode(message, examples, config, client=None):
        raise RuntimeError("provider down")

    agent = Agent(
        source=FixtureSource(FIXTURES),
        notifier=OffsetNotifier(),
        config=Config(data_dir=tmp_path, gate=GateConfig(threshold=0.5)),
        classify_fn=explode,
    )
    agent.cycle()

    assert State(tmp_path / "state.json").telegram_offset == 99
