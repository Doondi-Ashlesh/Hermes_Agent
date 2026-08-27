"""Configuration, resolved from the environment.

A `.env` file in the working directory is loaded if present. Nothing here has a
secret as a default, and nothing is written back.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_INTERVAL = 60
DEFAULT_THRESHOLD = 0.7


def load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from a simple KEY=VALUE file. Existing vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def _csv(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class GateConfig:
    """Deterministic notify/stay-silent rules. No model judgement involved."""

    threshold: float = DEFAULT_THRESHOLD
    always_senders: list[str] = field(default_factory=list)
    never_senders: list[str] = field(default_factory=list)
    always_keywords: list[str] = field(default_factory=list)
    muted_categories: list[str] = field(default_factory=list)
    quiet_start: int | None = None
    quiet_end: int | None = None

    @classmethod
    def from_env(cls) -> "GateConfig":
        quiet = os.environ.get("HERMES_QUIET_HOURS", "").strip()
        start = end = None
        if "-" in quiet:
            head, _, tail = quiet.partition("-")
            try:
                start, end = int(head), int(tail)
            except ValueError:
                start = end = None
        return cls(
            threshold=_float("HERMES_THRESHOLD", DEFAULT_THRESHOLD),
            always_senders=_csv("HERMES_ALWAYS_SENDERS"),
            never_senders=_csv("HERMES_NEVER_SENDERS"),
            always_keywords=_csv("HERMES_ALWAYS_KEYWORDS"),
            muted_categories=_csv("HERMES_MUTED_CATEGORIES"),
            quiet_start=start,
            quiet_end=end,
        )


@dataclass
class Config:
    model: str = DEFAULT_MODEL
    effort: str | None = None
    interval: int = DEFAULT_INTERVAL
    data_dir: Path = Path("data")
    max_examples: int = 40

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    telegram_token: str = ""
    telegram_chat_id: str = ""

    gate: GateConfig = field(default_factory=GateConfig)

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        return cls(
            model=os.environ.get("HERMES_MODEL", DEFAULT_MODEL),
            effort=os.environ.get("HERMES_EFFORT") or None,
            interval=_int("HERMES_INTERVAL", DEFAULT_INTERVAL),
            data_dir=Path(os.environ.get("HERMES_DATA_DIR", "data")),
            max_examples=_int("HERMES_MAX_EXAMPLES", 40),
            imap_host=os.environ.get("IMAP_HOST", ""),
            imap_port=_int("IMAP_PORT", 993),
            imap_user=os.environ.get("IMAP_USER", ""),
            imap_password=os.environ.get("IMAP_PASSWORD", ""),
            imap_folder=os.environ.get("IMAP_FOLDER", "INBOX"),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            gate=GateConfig.from_env(),
        )

    @property
    def has_imap(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def ensure_data_dir(self) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir
