"""CLI and offline-classifier smoke tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_inbox.cli import main
from hermes_inbox.offline import classify as offline_classify
from hermes_inbox.schema import Message

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "inbox.json"


def load_fixture(uid: str) -> Message:
    for item in json.loads(FIXTURES.read_text(encoding="utf-8")):
        if item["uid"] == uid:
            return Message.from_dict({**item, "source": "fixtures"})
    raise KeyError(uid)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("IMAP_HOST", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


# --------------------------------------------------------------------------- #
# offline classifier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uid,expect_notify",
    [
        ("101", False),  # newsletter
        ("102", True),   # lead reply asking for pricing
        ("103", True),   # payment failure
        ("104", False),  # marketing with fake urgency
        ("105", True),   # security alert
        ("106", False),  # bounce
        ("109", False),  # prompt injection attempt
        ("110", True),   # contract deadline
    ],
)
def test_offline_classifier_on_fixtures(uid, expect_notify):
    verdict = offline_classify(load_fixture(uid))
    assert (verdict.score >= 0.7) is expect_notify, verdict.reason


def test_injection_attempt_is_scored_as_spam():
    verdict = offline_classify(load_fixture("109"))
    assert verdict.category == "spam"
    assert verdict.score < 0.1


def test_offline_signature_matches_real_classifier():
    """Both must be interchangeable wherever `classify_fn` is accepted."""
    import inspect

    from hermes_inbox.classify import classify as real

    assert list(inspect.signature(real).parameters) == list(
        inspect.signature(offline_classify).parameters
    )


def test_bulk_headers_weight_a_score_down():
    plain = Message(
        uid="x", source="t", sender="a@b.example", subject="invoice past due",
        body="Your invoice is past due.", received_at=datetime.now(timezone.utc),
    )
    bulk = Message(
        uid="y", source="t", sender="a@b.example", subject="invoice past due",
        body="Your invoice is past due.", received_at=datetime.now(timezone.utc),
        headers={"Precedence": "bulk"},
    )
    assert offline_classify(bulk).score < offline_classify(plain).score


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_demo_runs_clean(capsys):
    assert main(["demo", "--offline", "--data-dir", "data/demo"]) == 0
    out = capsys.readouterr().out
    assert "12 fixture messages" in out
    assert "would have interrupted you" in out


def test_once_with_fixtures(capsys):
    assert main(["once", "--fixtures", "--console", "--offline"]) == 0
    assert "fetched" in capsys.readouterr().out


def test_stats_before_any_run(capsys):
    assert main(["stats"]) == 0
    assert "nothing processed yet" in capsys.readouterr().out


def test_feedback_then_eval_then_stats(capsys):
    main(["once", "--fixtures", "--console", "--offline"])
    capsys.readouterr()

    assert main(["feedback", "104", "not-important", "--note", "never ping me for sales"]) == 0
    assert "not important" in capsys.readouterr().out

    assert main(["eval", "--offline"]) == 0
    assert "Replayed 1 labeled example" in capsys.readouterr().out

    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "12 messages processed" in out
    assert "by gate rule" in out


def test_feedback_for_unknown_uid_exits_nonzero(capsys):
    assert main(["feedback", "does-not-exist", "important"]) == 1
    assert "no decision recorded" in capsys.readouterr().err


def test_threshold_override_changes_notification_count(capsys):
    main(["once", "--fixtures", "--console", "--offline", "--threshold", "0.99"])
    strict = capsys.readouterr().out
    assert "0 notified" in strict


def test_eval_with_no_corrections(capsys):
    assert main(["eval", "--offline"]) == 0
    assert "No labeled examples" in capsys.readouterr().out
