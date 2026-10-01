"""Preflight checks.

Two properties matter more than the individual checks: it must never print a
secret, and it must never change anything. Both are asserted directly.
"""

from __future__ import annotations

import pytest

from hermes_inbox import doctor
from hermes_inbox.cli import main
from hermes_inbox.config import Config, GateConfig
from hermes_inbox.feedback import Example, FeedbackStore

SECRET = "sk-ant-supersecretvalue12345"
APP_PASSWORD = "abcdefghijklmnop"
BOT_TOKEN = "1234567890:AAHrealbottokenvalue"


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    """No network, no credentials, no stray .env from the repo root."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(doctor, "_reachable", lambda host, port, timeout=5.0: (True, "reachable"))
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "IMAP_HOST", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: False)


def configured(tmp_path, **overrides) -> Config:
    defaults = dict(
        data_dir=tmp_path / "data",
        provider="anthropic",
        imap_host="imap.gmail.com",
        imap_user="me@example.com",
        imap_password=APP_PASSWORD,
        telegram_token=BOT_TOKEN,
        telegram_chat_id="987654321",
        gate=GateConfig(threshold=0.7),
    )
    defaults.update(overrides)
    return Config(**defaults)


def status_of(report, name):
    return next(c.status for c in report.checks if c.name == name)


# --------------------------------------------------------------------------- #
# it must never leak a secret
# --------------------------------------------------------------------------- #


def test_no_secret_appears_anywhere_in_the_report(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)

    rendered = doctor.run(configured(tmp_path), login=False).render()

    assert SECRET not in rendered
    assert APP_PASSWORD not in rendered
    assert BOT_TOKEN not in rendered


def test_mask_keeps_only_a_recognisable_tail():
    token = "1234567890:AAHsecret"
    masked = doctor.mask(token)

    assert len(masked) == len(token), "length is preserved so it still looks like a token"
    assert masked.endswith("cret")
    assert masked[:-4] == "*" * (len(token) - 4)
    assert token[:-4] not in masked

    assert doctor.mask("abcd") == "****", "short values reveal nothing"
    assert doctor.mask("") == ""


def test_doctor_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    before = sorted(p.name for p in tmp_path.iterdir())
    doctor.run(configured(tmp_path), login=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# --------------------------------------------------------------------------- #
# the checks
# --------------------------------------------------------------------------- #


def test_missing_credential_fails(tmp_path):
    report = doctor.run(configured(tmp_path), login=False)
    assert status_of(report, "model credential") == doctor.FAIL
    assert not report.ready


def test_offline_provider_is_a_failure_when_it_was_not_asked_for(tmp_path):
    report = doctor.run(configured(tmp_path, provider="auto"), login=False)
    assert status_of(report, "model provider") == doctor.FAIL


def test_offline_provider_is_only_a_warning_when_chosen(tmp_path):
    report = doctor.run(configured(tmp_path, provider="offline"), login=False)
    assert status_of(report, "model provider") == doctor.WARN


def test_unconfigured_mailbox_names_what_is_missing(tmp_path):
    report = doctor.run(configured(tmp_path, imap_host="", imap_password=""), login=False)
    check = next(c for c in report.checks if c.name == "mailbox")
    assert check.status == doctor.FAIL
    assert "IMAP_HOST" in check.detail and "IMAP_PASSWORD" in check.detail
    assert "App Password" in check.fix


def test_unreachable_mailbox_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_reachable", lambda h, p, timeout=5.0: (False, "TimeoutError"))
    report = doctor.run(configured(tmp_path), login=False)
    assert status_of(report, "mailbox reachable") == doctor.FAIL


def test_ollama_down_is_reported_with_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_reachable", lambda h, p, timeout=5.0: (False, "ConnectionRefusedError"))
    report = doctor.run(configured(tmp_path, provider="ollama"), login=False)
    check = next(c for c in report.checks if c.name == "model provider")
    assert check.status == doctor.FAIL
    assert "ollama serve" in check.fix


def test_openai_compat_probes_the_configured_endpoint(tmp_path, monkeypatch):
    probed = []

    def down(host, port, timeout=5.0):
        probed.append((host, port))
        return False, "ConnectionRefusedError"

    monkeypatch.setattr(doctor, "_reachable", down)
    config = configured(
        tmp_path,
        provider="openai-compat",
        openai_base_url="http://gpu-box:8000/v1",
        openai_api_key="nvapi-secretvalue987",
    )
    report = doctor.run(config, login=False)
    check = next(c for c in report.checks if c.name == "model provider")
    assert ("gpu-box", 8000) in probed
    assert check.status == doctor.FAIL
    assert "vllm serve" in check.fix
    assert "nvapi-secretvalue987" not in report.render()


def test_missing_notifier_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    """Console output is a perfectly good fallback; it must not block a run."""
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    report = doctor.run(configured(tmp_path, telegram_token=""), login=False)
    assert status_of(report, "notifier") == doctor.WARN
    assert report.ready


def test_malformed_bot_token_is_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    report = doctor.run(configured(tmp_path, telegram_token="not-a-token"), login=False)
    assert status_of(report, "notifier") == doctor.WARN


def test_gate_summary_reflects_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    gate = GateConfig(threshold=0.55, always_senders=["a@b.example"], quiet_start=22, quiet_end=7)
    report = doctor.run(configured(tmp_path, gate=gate), login=False)
    detail = next(c.detail for c in report.checks if c.name == "policy gate")
    assert "0.55" in detail and "always 1" in detail and "quiet 22-7" in detail


def test_correction_count_drives_the_advice(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    data = tmp_path / "data"
    data.mkdir()
    store = FeedbackStore(data / "feedback.jsonl")

    report = doctor.run(configured(tmp_path), login=False)
    assert status_of(report, "corrections") == doctor.WARN

    for i in range(30):
        store.add(Example(str(i), "a@b.example", "s", "snip", i % 2 == 0))

    report = doctor.run(configured(tmp_path), login=False)
    check = next(c for c in report.checks if c.name == "corrections")
    assert check.status == doctor.OK
    assert "30" in check.detail


def test_a_secret_left_in_dotenv_after_storing_it_is_flagged(tmp_path, monkeypatch):
    """The half-finished migration. `.env` wins, so nothing else would say so."""
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    (tmp_path / ".env").write_text(f"IMAP_PASSWORD={APP_PASSWORD}\n", encoding="utf-8")
    monkeypatch.setattr("hermes_inbox.secrets.available", lambda: (True, "fake"))
    monkeypatch.setattr("hermes_inbox.secrets.get", lambda name: "stored-copy")

    report = doctor.run(configured(tmp_path), login=False)
    check = next(c for c in report.checks if "IMAP_PASSWORD" in c.name)

    assert check.status == doctor.WARN
    assert ".env" in check.fix
    assert APP_PASSWORD not in report.render(), "the warning must not quote the value"


def test_no_keychain_is_skipped_rather_than_failed(tmp_path, monkeypatch):
    """Most installs will never have one. That is not a problem to report."""
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    monkeypatch.setattr("hermes_inbox.secrets.available", lambda: (False, "not installed"))

    report = doctor.run(configured(tmp_path), login=False)
    assert status_of(report, "keychain") == doctor.SKIP
    assert report.ready


def test_secrets_check_names_the_source_of_each(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setattr("hermes_inbox.secrets.available", lambda: (False, "not installed"))

    detail = next(
        c.detail for c in doctor.run(configured(tmp_path), login=False).checks
        if c.name == "secrets"
    )
    assert "ANTHROPIC_API_KEY ← environment" in detail
    assert SECRET not in detail


# --------------------------------------------------------------------------- #
# the next-step advice
# --------------------------------------------------------------------------- #


def test_next_step_points_at_the_first_failure(tmp_path):
    config = configured(tmp_path)
    report = doctor.run(config, login=False)
    assert "ANTHROPIC_API_KEY" in doctor.next_step(report, config)


def test_next_step_is_eval_once_there_are_enough_corrections(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    data = tmp_path / "data"
    data.mkdir()
    store = FeedbackStore(data / "feedback.jsonl")
    for i in range(30):
        store.add(Example(str(i), "a@b.example", "s", "snip", True))

    config = configured(tmp_path)
    assert doctor.next_step(doctor.run(config, login=False), config) == "make eval"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_exits_nonzero_when_something_is_broken(capsys, monkeypatch):
    monkeypatch.setenv("HERMES_DATA_DIR", "data")
    assert main(["doctor", "--no-login"]) == 1
    out = capsys.readouterr().out
    assert "to fix before it can run" in out
    assert "next:" in out


def test_cli_reports_ready_when_it_is(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("IMAP_HOST", "imap.gmail.com")
    monkeypatch.setenv("IMAP_USER", "me@example.com")
    monkeypatch.setenv("IMAP_PASSWORD", APP_PASSWORD)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "data"))

    assert main(["doctor", "--no-login"]) == 0
    out = capsys.readouterr().out
    assert "ready to run" in out
    assert SECRET not in out and APP_PASSWORD not in out
