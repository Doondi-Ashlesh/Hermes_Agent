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


# --------------------------------------------------------------------------- #
# the guides must stay true to the code
# --------------------------------------------------------------------------- #


def test_makefile_targets_referenced_in_docs_exist():
    """`make X` in a doc must be a real target."""
    makefile = read("Makefile")
    targets = set(re.findall(r"^([a-z-]+):", makefile, re.M))
    for doc in ("README.md", "docs/SETUP.md", "docs/EXTENDING.md", "docs/INBOX_AGENT.md"):
        for target in re.findall(r"make ([a-z][a-z-]*)", read(doc)):  # not `make --version`
            assert target in targets, f"{doc} references `make {target}`, not in Makefile"


def test_makefile_targets_are_self_documented():
    """Every target carries a ## comment so `make help` is complete."""
    undocumented = [
        line.split(":")[0]
        for line in read("Makefile").splitlines()
        if re.match(r"^[a-z-]+:", line) and "##" not in line
    ]
    assert not undocumented, f"Makefile targets missing a ## description: {undocumented}"


@pytest.mark.parametrize(
    "module,symbol",
    [
        ("hermes_inbox.classify", "build_system"),
        ("hermes_inbox.classify", "build_user"),
        ("hermes_inbox.classify", "SCHEMA"),
        ("hermes_inbox.redact", "redact_message"),
        ("hermes_inbox.gate", "decide"),
        ("hermes_inbox.providers", "resolve"),
        ("hermes_inbox.providers", "NAMES"),
        ("hermes_inbox.schema", "GateDecision"),
        ("hermes_inbox.schema", "Verdict"),
    ],
)
def test_extending_guide_names_real_symbols(module, symbol):
    """Every symbol the extension recipes tell a dev to import must exist."""
    import importlib

    guide = read("docs/EXTENDING.md")
    assert symbol in guide, f"EXTENDING.md no longer mentions {symbol}"
    assert hasattr(importlib.import_module(module), symbol), (
        f"EXTENDING.md tells devs to use {module}.{symbol}, which does not exist"
    )


def test_extending_guide_documents_the_real_protocol_methods():
    """The method names in the recipes must match the protocols."""
    guide = read("docs/EXTENDING.md")
    for method in ("fetch_new", "send", "poll_feedback", "classify"):
        assert method in guide, f"EXTENDING.md does not cover {method}"

    source_sig = read("hermes_inbox/sources/base.py")
    assert "since_uid" in guide and "since_uid" in source_sig
    notify_sig = read("hermes_inbox/notify/base.py")
    assert "offset" in guide and "offset" in notify_sig


def test_module_inventory_matches_the_package():
    """The file listing at the end of EXTENDING.md must not go stale."""
    guide = read("docs/EXTENDING.md")
    actual = {p.name for p in (ROOT / "hermes_inbox").glob("*.py")} - {"__init__.py"}
    missing = {name for name in actual if name not in guide}
    assert not missing, f"modules absent from the EXTENDING.md inventory: {sorted(missing)}"


def test_setup_guide_uses_real_env_vars():
    """Every KEY=value in SETUP.md is a key config.py actually reads."""
    config = read("hermes_inbox/config.py")
    known = set(re.findall(r'"([A-Z_]+)"', config)) | {"ANTHROPIC_API_KEY"}
    used = set(re.findall(r"^([A-Z][A-Z_]{3,})=", read("docs/SETUP.md"), re.M))
    unknown = used - known
    assert not unknown, f"SETUP.md sets env vars nothing reads: {sorted(unknown)}"


def test_setup_guide_has_a_verification_for_each_step():
    """Each numbered step must tell the reader what success looks like."""
    setup = read("docs/SETUP.md")
    steps = re.findall(r"^## Step \d+ — .*$", setup, re.M)
    assert len(steps) >= 5, "SETUP.md should walk through at least 5 steps"
    assert setup.count("**Verify") >= 4, "each setup step needs a verification"


def test_troubleshooting_covers_the_known_failure_modes():
    setup = read("docs/SETUP.md")
    for symptom in ("ollama serve", "app password", "state.json", "offline keyword rules"):
        assert symptom in setup, f"troubleshooting missing: {symptom}"


def _collected_test_count() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(ROOT / "tests")],
        capture_output=True, text=True, cwd=ROOT,
    ).stdout
    match = re.search(r"(\d+) tests? collected", out)
    assert match, f"could not read the collected test count:\n{out[-500:]}"
    return int(match.group(1))


def test_documented_test_counts_are_accurate():
    """Any doc quoting "N tests" or "N passed" must quote the real number.

    Cheap to keep true, and a wrong number is the kind of small lie that makes a
    reader distrust the rest of the page.
    """
    actual = _collected_test_count()
    wrong = []
    for doc in ("README.md", "CLAUDE.md", "docs/SETUP.md", "docs/EXTENDING.md",
                "docs/INBOX_AGENT.md", "docs/DECISIONS.md"):
        for claimed in re.findall(r"(\d+)\s+(?:tests?|passed)\b", read(doc)):
            if int(claimed) != actual:
                wrong.append(f"{doc} says {claimed}, actual is {actual}")
    assert not wrong, "stale test counts:\n" + "\n".join(wrong)
