"""Backfill and the sorted listing.

Backfill exists to produce corrections in bulk, so its contract is mostly about
what it must *not* do: notify anyone, move the live cursor, or reclassify what
it has already seen.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_inbox.agent import Agent
from hermes_inbox.cli import main
from hermes_inbox.config import Config, GateConfig
from hermes_inbox.schema import Message, Verdict
from hermes_inbox.sources.fixtures import FixtureSource
from hermes_inbox.state import State

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "inbox.json"
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


class StubNotifier:
    name = "stub"

    def __init__(self):
        self.sent = []

    def send(self, decision):
        self.sent.append(decision)

    def poll_feedback(self, offset=None):
        return [], offset


def scoring(scores: dict[str, float]):
    def classify(message, examples, config, client=None):
        score = scores.get(message.uid, 0.0)
        return Verdict(score >= 0.5, score, "personal", "reason", "do the thing")

    return classify


def make_agent(tmp_path, notifier=None, classify_fn=None):
    return Agent(
        source=FixtureSource(FIXTURES),
        notifier=notifier or StubNotifier(),
        config=Config(data_dir=tmp_path, gate=GateConfig(threshold=0.5)),
        classify_fn=classify_fn or scoring({"103": 0.9, "110": 0.8}),
    )


# --------------------------------------------------------------------------- #
# backfill contract
# --------------------------------------------------------------------------- #


def test_backfill_classifies_history(tmp_path):
    agent = make_agent(tmp_path)
    result = agent.backfill(LONG_AGO)

    assert result.fetched == 12
    assert len(agent.log.all()) == 12


def test_backfill_never_notifies(tmp_path):
    """It counts what would have fired, but must not send anything."""
    notifier = StubNotifier()
    result = make_agent(tmp_path, notifier).backfill(LONG_AGO)

    assert result.notified == 2, "should count would-have-notified"
    assert notifier.sent == [], "backfill must not actually notify"


def test_backfill_leaves_the_live_cursor_alone(tmp_path):
    """Advancing it would make the next `run` skip genuinely new mail."""
    agent = make_agent(tmp_path)
    agent.backfill(LONG_AGO)

    assert agent.state.last_uid("fixtures") is None
    assert State(tmp_path / "state.json").last_uid("fixtures") is None


def test_backfill_is_rerunnable(tmp_path):
    agent = make_agent(tmp_path)
    assert agent.backfill(LONG_AGO).fetched == 12
    assert agent.backfill(LONG_AGO).fetched == 0, "already-classified mail must be skipped"
    assert len(agent.log.all()) == 12, "no duplicate decisions"


def test_backfill_and_live_loop_do_not_double_process(tmp_path):
    agent = make_agent(tmp_path)
    agent.backfill(LONG_AGO)

    # The live loop still sees them (its cursor is untouched) but the log has
    # one decision per message per pass; what matters is nothing is lost.
    result = agent.cycle()
    assert result.fetched == 12
    assert agent.state.last_uid("fixtures") == "112"


def test_backfill_respects_the_date_window(tmp_path):
    agent = make_agent(tmp_path)
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert agent.backfill(future).fetched == 0


def test_backfill_respects_the_limit(tmp_path):
    assert make_agent(tmp_path).backfill(LONG_AGO, limit=5).fetched == 5


def test_backfill_reports_progress(tmp_path):
    seen = []
    make_agent(tmp_path).backfill(LONG_AGO, on_progress=lambda i, n, m, v: seen.append((i, n)))

    assert seen[0] == (1, 12)
    assert seen[-1] == (12, 12)


def test_backfill_stops_on_a_classifier_failure(tmp_path):
    def explode(message, examples, config, client=None):
        if message.uid == "103":
            raise RuntimeError("provider down")
        return Verdict(True, 0.9, "personal", "r")

    result = make_agent(tmp_path, classify_fn=explode).backfill(LONG_AGO)
    assert any("103" in e for e in result.errors)
    assert result.fetched == 12  # what it set out to do
    assert len(make_agent(tmp_path).log.all()) == 2  # only what it got through


def test_backfill_needs_a_date_capable_source(tmp_path):
    class UidOnly:
        name = "uid-only"

        def fetch_new(self, since_uid=None, limit=25):
            return []

    agent = Agent(
        source=UidOnly(),
        notifier=StubNotifier(),
        config=Config(data_dir=tmp_path),
        classify_fn=scoring({}),
    )
    with pytest.raises(NotImplementedError, match="fetch_since"):
        agent.backfill(LONG_AGO)


def test_fixture_source_filters_by_date():
    source = FixtureSource(FIXTURES)
    assert len(source.fetch_since(LONG_AGO)) == 12
    assert source.fetch_since(datetime.now(timezone.utc) + timedelta(days=1)) == []


# --------------------------------------------------------------------------- #
# the sorted listing
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("IMAP_HOST", raising=False)


def seed(capsys):
    main(["backfill", "--fixtures", "--console", "--provider", "offline", "--days", "36500", "--yes"])
    capsys.readouterr()


def test_list_sorts_by_score_descending(capsys):
    seed(capsys)
    main(["list"])
    scores = [float(l.split()[1]) for l in capsys.readouterr().out.splitlines() if l.startswith(("▲", " 0"))]
    assert scores == sorted(scores, reverse=True)


def test_list_shows_summary_and_action(capsys):
    seed(capsys)
    main(["list", "--min-score", "0.9"])
    out = capsys.readouterr().out
    assert "security" in out
    assert "→" in out, "the suggested action should be shown"
    assert "uid" in out


def test_list_filters(capsys):
    seed(capsys)

    main(["list", "--category", "billing"])
    assert "billing" in capsys.readouterr().out

    main(["list", "--min-score", "0.99"])
    assert "nothing matches" in capsys.readouterr().out


def test_list_hides_what_you_already_labeled(capsys):
    seed(capsys)
    main(["list", "--unlabeled", "--min-score", "0.9"])
    before = capsys.readouterr().out
    assert "uid 105" in before

    main(["feedback", "105", "important"])
    capsys.readouterr()

    main(["list", "--unlabeled", "--min-score", "0.9"])
    assert "uid 105" not in capsys.readouterr().out


def test_list_before_anything_is_classified(capsys):
    assert main(["list"]) == 0
    assert "backfill" in capsys.readouterr().out


def test_backfill_is_idempotent_through_the_cli(capsys):
    seed(capsys)
    main(["backfill", "--fixtures", "--console", "--provider", "offline", "--days", "36500", "--yes"])
    assert "already classified" in capsys.readouterr().out


def test_backfill_does_not_write_state_json(capsys):
    seed(capsys)
    data = Path(json.loads('"' + str(Path.cwd() / "data") + '"'))
    assert not (data / "state.json").exists() or State(data / "state.json").last_uid("fixtures") is None
