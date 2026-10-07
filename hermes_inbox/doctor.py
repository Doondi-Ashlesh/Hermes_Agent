"""Preflight checks.

`hermes-inbox doctor` answers one question: what is stopping this from running,
and what do I type next. It is the first thing to reach for when `once` fails,
and it works before any credential exists — which is when the answer matters
most.

Two rules it never breaks:

- **No secret is ever printed.** Values are reported as present or absent, with
  at most a masked fragment for identification.
- **Nothing is mutated.** Every check reads; none configures, writes, or sends.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .secrets import KEYRING

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"

_MARK = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "–"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name, status, detail="", fix="") -> None:
        self.checks.append(Check(name, status, detail, fix))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def ready(self) -> bool:
        return not self.failures

    def render(self) -> str:
        width = max(len(c.name) for c in self.checks)
        lines = []
        for check in self.checks:
            lines.append(f"  {_MARK[check.status]} {check.name:<{width}}  {check.detail}")
            if check.fix and check.status in (FAIL, WARN):
                lines.append(f"    {'':<{width}}    → {check.fix}")
        return "\n".join(lines)


def mask(value: str, keep: int = 4) -> str:
    """Enough to recognise a value, never enough to use it."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{'*' * (len(value) - keep)}{value[-keep:]}"


def _reachable(host: str, port: int, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True, "reachable"
    except Exception as exc:
        return False, f"{type(exc).__name__}"


def _endpoint(url: str, default_port: int) -> tuple[str, int]:
    """Host and port from a URL, however it is written.

    Parsed as a URL rather than split on ":" by hand, which crashed on a
    trailing slash (`http://localhost:11434/`, F-018).
    """
    from urllib.parse import urlparse

    parsed = urlparse(url if "://" in url else f"http://{url}")
    return parsed.hostname or "", parsed.port or default_port


def check_provider(config: Config, report: Report) -> None:
    from . import providers
    from .offline import has_credentials

    _, name = providers.resolve(config.provider)

    if name == "anthropic":
        report.add("model provider", OK, f"anthropic · {config.model}")
    elif name == "ollama":
        hostname, port = _endpoint(config.ollama_host, 11434)
        up, detail = _reachable(hostname, port, timeout=2)
        report.add(
            "model provider",
            OK if up else FAIL,
            f"ollama · {config.ollama_model} · {detail}",
            "" if up else "start it with: ollama serve",
        )
    elif name == "openai-compat":
        default = 443 if config.openai_base_url.startswith("https") else 80
        hostname, port = _endpoint(config.openai_base_url, default)
        up, detail = _reachable(hostname, port, timeout=2)
        report.add(
            "model provider",
            OK if up else FAIL,
            f"openai-compat · {config.openai_model or 'served model'} · {hostname}:{port}"
            f" · {detail}",
            ""
            if up
            else "start the server (e.g. vllm serve <model>), or fix HERMES_OPENAI_BASE_URL",
        )
    else:
        report.add(
            "model provider",
            FAIL if config.provider != "offline" else WARN,
            "offline keyword rules — does not learn from corrections",
            "set ANTHROPIC_API_KEY in .env, or HERMES_PROVIDER=ollama for a free local model",
        )

    if name == "anthropic" and not has_credentials():
        report.add("model credential", FAIL, "none resolvable", "put ANTHROPIC_API_KEY in .env")


def check_mailbox(config: Config, report: Report, login: bool = True) -> None:
    if not config.has_imap:
        missing = [
            key
            for key, value in (
                ("IMAP_HOST", config.imap_host),
                ("IMAP_USER", config.imap_user),
                ("IMAP_PASSWORD", config.imap_password),
            )
            if not value
        ]
        report.add(
            "mailbox",
            FAIL,
            f"not configured — missing {', '.join(missing)}",
            "Gmail needs a 16-char App Password, not your account password (SETUP.md §3b)",
        )
        return

    up, detail = _reachable(config.imap_host, config.imap_port)
    report.add(
        "mailbox reachable",
        OK if up else FAIL,
        f"{config.imap_host}:{config.imap_port} — {detail}",
        "" if up else "check the host, or whether outbound 993 is blocked here",
    )
    if not up or not login:
        return

    # A single login attempt: the wrong-password case is the most common setup
    # failure and nothing else detects it.
    import imaplib

    try:
        with imaplib.IMAP4_SSL(config.imap_host, config.imap_port) as conn:
            conn.login(config.imap_user, config.imap_password)
            status, data = conn.select(config.imap_folder, readonly=True)
            count = int(data[0]) if status == "OK" and data and data[0] else 0
        report.add(
            "mailbox login",
            OK,
            f"{config.imap_user} · {config.imap_folder} · {count} message(s)",
        )
    except Exception as exc:
        text = str(exc)
        hint = (
            "that looks like an account password; Gmail needs an App Password (SETUP.md §3b)"
            if "AUTHENTICATIONFAILED" in text.upper() or "Invalid credentials" in text
            else "check IMAP_USER / IMAP_PASSWORD / IMAP_FOLDER"
        )
        report.add("mailbox login", FAIL, text[:80], hint)


def check_notifier(config: Config, report: Report) -> None:
    if not config.has_telegram:
        report.add(
            "notifier",
            WARN,
            "telegram not configured — alerts will print to the console",
            "optional; SETUP.md §3c if you want them on your phone",
        )
        return
    token = config.telegram_token
    shape_ok = ":" in token and token.split(":")[0].isdigit()
    report.add(
        "notifier",
        OK if shape_ok else WARN,
        f"telegram · token {mask(token)} · chat {config.telegram_chat_id}",
        "" if shape_ok else "a bot token looks like 1234567890:AA... — check it was copied whole",
    )


def check_secrets(config: Config, report: Report) -> None:
    """Where each secret comes from — never what it is.

    The shadow warning is the point of this check. `.env` outranks the keychain,
    so a secret left in the file after being stored in the keychain is still the
    one being used, and nothing else would ever tell you.
    """
    from . import secrets

    usable, detail = secrets.available()
    resolutions = secrets.inspect()

    report.add("keychain", OK if usable else SKIP, detail)

    found = [r for r in resolutions if r.present]
    report.add(
        "secrets",
        OK if found else WARN,
        " · ".join(f"{r.name} ← {r.source}" for r in found) or "none resolved",
        "" if found else "SETUP.md §3 — nothing is configured yet",
    )

    for shadowed in (r for r in resolutions if KEYRING in r.shadowed):
        report.add(
            f"  {shadowed.name}",
            WARN,
            f"in the keychain, but {shadowed.source} wins",
            f"remove {shadowed.name} from {shadowed.source} to use the stored one",
        )


def check_gate(config: Config, report: Report) -> None:
    gate = config.gate
    bits = [f"threshold {gate.threshold:g}"]
    for label, values in (
        ("always", gate.always_senders),
        ("never", gate.never_senders),
        ("muted", gate.muted_categories),
    ):
        if values:
            bits.append(f"{label} {len(values)}")
    if gate.quiet_start is not None:
        bits.append(f"quiet {gate.quiet_start}-{gate.quiet_end}")
    report.add("policy gate", OK, " · ".join(bits))


def check_data(config: Config, report: Report) -> None:
    from .feedback import FeedbackStore
    from .state import DecisionLog

    data = config.data_dir
    if not data.exists():
        report.add(
            "history",
            WARN,
            "no decisions yet",
            "hermes-inbox backfill --days 30 to classify existing mail",
        )
        return

    decisions = sum(1 for _ in DecisionLog(data / "decisions.jsonl").iter_all())
    important, not_important = FeedbackStore(data / "feedback.jsonl").counts()
    total = important + not_important

    report.add("history", OK, f"{decisions} decision(s) in {data}")

    if total >= 30:
        report.add("corrections", OK, f"{total} ({important} important) — enough to evaluate")
    elif total:
        report.add(
            "corrections",
            WARN,
            f"{total} — an eval score needs about 30 to mean anything",
            "hermes-inbox list --unlabeled --min-score 0.5",
        )
    else:
        report.add(
            "corrections",
            WARN,
            "none yet — the agent cannot learn your judgement without them",
            "hermes-inbox list --unlabeled  then  hermes-inbox feedback <uid> ...",
        )


def run(config: Config | None = None, login: bool = True) -> Report:
    """Every check, in the order a first-time setup hits them."""
    config = config or Config.from_env()
    report = Report()

    env = Path(".env")
    report.add(
        ".env",
        OK if env.is_file() else WARN,
        "found" if env.is_file() else "not found — using environment and defaults",
        "" if env.is_file() else "cp .env.example .env",
    )

    check_secrets(config, report)
    check_provider(config, report)
    check_mailbox(config, report, login=login)
    check_notifier(config, report)
    check_gate(config, report)
    check_data(config, report)
    return report


def next_step(report: Report, config: Config) -> str:
    """The single most useful thing to type next."""
    if report.failures:
        first = report.failures[0]
        return first.fix or f"fix: {first.name}"

    corrections = next((c for c in report.checks if c.name == "corrections"), None)
    if corrections and corrections.status == WARN:
        return corrections.fix
    if corrections and corrections.status == OK:
        return "make eval"
    return "hermes-inbox once --console"
