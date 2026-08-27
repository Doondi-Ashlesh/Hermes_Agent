"""Documentation consistency.

CLAUDE.md §2 says a change must update every file it touches. Remembering to do
that is unreliable, so the parts that can be checked mechanically are checked
here — a stale doc fails the build like any other bug.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_internal_links_and_anchors_resolve():
    """Every relative markdown link points at a file, and every anchor exists."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_links.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"broken cross-references:\n{result.stderr}"


def test_every_config_env_var_is_documented():
    """A new HERMES_*/IMAP_*/TELEGRAM_* key must appear in .env.example."""
    config = read("hermes_inbox/config.py")
    declared = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', config))
    declared |= set(re.findall(r'_(?:int|float|csv)\(\s*"([A-Z_]+)"', config))
    declared -= {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}  # SDK-resolved

    documented = set(re.findall(r"^#?\s*([A-Z_]+)=", read(".env.example"), re.M))
    missing = declared - documented
    assert not missing, f"env vars read by config.py but absent from .env.example: {sorted(missing)}"


def test_no_stale_env_vars_in_example():
    """And the reverse: .env.example must not advertise keys nothing reads."""
    config = read("hermes_inbox/config.py")
    documented = set(re.findall(r"^#?\s*([A-Z_]+)=", read(".env.example"), re.M))
    documented -= {"ANTHROPIC_API_KEY"}
    unread = {key for key in documented if key not in config}
    assert not unread, f".env.example documents keys nothing reads: {sorted(unread)}"


def test_cli_commands_are_documented():
    """Every subcommand appears in the INBOX_AGENT command table."""
    cli = read("hermes_inbox/cli.py")
    commands = set(re.findall(r'sub\.add_parser\(\s*"([a-z-]+)"', cli))
    doc = read("docs/INBOX_AGENT.md")
    missing = {c for c in commands if f"hermes-inbox {c}" not in doc}
    assert not missing, f"undocumented CLI commands: {sorted(missing)}"


def test_every_provider_is_documented():
    from hermes_inbox.providers import NAMES

    doc = read("docs/INBOX_AGENT.md")
    missing = {name for name in NAMES if f"`{name}`" not in doc}
    assert not missing, f"undocumented providers: {sorted(missing)}"


def test_default_model_matches_the_documented_default():
    """The cost table must name the model the code actually defaults to."""
    from hermes_inbox.config import DEFAULT_MODEL

    doc = read("docs/INBOX_AGENT.md")
    row = re.search(r"\|\s*\*\*(.+?)\*\*\s*\*\(default\)\*", doc)
    assert row, "no model marked as default in the cost table"
    documented = row.group(1).strip().lower().replace(" ", "-")
    assert documented in DEFAULT_MODEL, (
        f"docs say the default is {row.group(1)!r}, code says {DEFAULT_MODEL!r}"
    )


def test_decision_log_entries_are_well_formed():
    """Every entry has an id of a known type, and ids are unique."""
    log = read("docs/DECISIONS.md")
    ids = re.findall(r"^### ([DFO]-\d{3}) · ", log, re.M)
    assert ids, "decision log has no entries"
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}"
    for prefix in ("D", "F", "O"):
        assert any(i.startswith(prefix) for i in ids), f"no {prefix}-nnn entries"


@pytest.mark.parametrize(
    "doc,must_mention",
    [
        ("docs/PLAN.md", "DECISIONS.md"),
        ("docs/ARCHITECTURE.md", "INBOX_AGENT.md"),
        ("docs/INBOX_AGENT.md", "ARCHITECTURE.md"),
        ("README.md", "docs/INBOX_AGENT.md"),
    ],
)
def test_documents_reference_each_other(doc, must_mention):
    """Keeps the doc set navigable rather than a pile of orphans."""
    assert must_mention in read(doc), f"{doc} should link to {must_mention}"


def test_adr_amendment_is_reflected_in_the_plan():
    """A resolved ADR question must not still read as open in PLAN.md."""
    adr = read("docs/adr/0001-runtime-nemoclaw-hermes.md")
    plan = read("docs/PLAN.md")
    if "Amendment 2026-08-27" in adr:
        assert "resolved 2026-08-27" in plan, (
            "ADR 0001 was amended but PLAN.md still lists those decisions as open"
        )


def test_known_limits_are_documented():
    """Behaviours a user would be surprised by must be written down."""
    doc = read("docs/INBOX_AGENT.md")
    for topic in ("Polling", "cursor", "HERMES_MAX_EXAMPLES"):
        assert topic in doc, f"known limit not documented: {topic}"
