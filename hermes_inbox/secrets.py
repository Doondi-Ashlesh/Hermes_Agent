"""Where the three secrets come from.

Exactly three values are secret: the model key, the mailbox password, and the
bot token. By default they sit in plaintext in `.env`, which is defensible on a
laptop you alone use and poor anywhere else — `.env` survives backups, syncs to
cloud folders, and is readable by anything running as you.

`keyring` puts them in the OS keychain instead: Keychain on macOS, the
Credential Manager on Windows, Secret Service on a Linux desktop.

Resolution order, highest first:

1. a real environment variable — how containers and systemd units inject them
2. `.env`
3. the OS keychain

The keychain is **last on purpose**. It means an existing `.env` install keeps
behaving exactly as it did, the keychain is only consulted for values nothing
else supplied (so no DBus round-trip and no unlock prompt when it is unused),
and a half-finished migration keeps working from `.env` instead of silently
reading a stale copy. The cost is that a value left in `.env` shadows the
keychain one — `doctor` warns when that happens, because otherwise you would
think you had moved a secret that you had not.

Nothing here ever logs, prints, or returns a secret to a caller that did not
ask for that specific name, and `set` reads values from a TTY prompt rather
than argv, which would otherwise land in shell history and `ps`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "hermes-inbox"

#: The only values worth protecting. Everything else in `Config` is a hostname,
#: a threshold, or a preference.
SECRETS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "model key",
    "IMAP_PASSWORD": "mailbox app password",
    "TELEGRAM_BOT_TOKEN": "bot token",
}

ENVIRONMENT, DOTENV, KEYRING = "environment", ".env", "keyring"

#: Names this process copied out of the keychain into `os.environ`. Without it
#: `inspect` would see its own injection and report the secret as coming from
#: the environment, which is exactly backwards.
_injected: set[str] = set()


def enabled() -> bool:
    return os.environ.get("HERMES_KEYRING", "auto").strip().lower() != "off"


def available() -> tuple[bool, str]:
    """Is there a keychain we can actually use, and what is it called?

    Importable is not the same as usable. On a headless Linux box — which is
    where this agent most often runs — `keyring` installs a backend whose every
    method raises, so the check has to look at which backend was selected, not
    just whether the import worked.
    """
    if not enabled():
        return False, "disabled by HERMES_KEYRING=off"
    try:
        import keyring
    except ImportError:
        return False, "not installed — pip install 'hermes-inbox[keyring]'"
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # a broken config file, a missing DBus session
        return False, f"unusable — {type(exc).__name__}"

    module = type(backend).__module__
    if ".fail" in module or ".null" in module:
        return False, "no OS keychain here (headless?) — .env is the fallback"
    return True, str(getattr(backend, "name", type(backend).__name__))


def get(name: str) -> str | None:
    """The stored value, or None. Never raises.

    A keychain that is locked, broken, or absent must not stop the agent from
    starting — it falls through to whatever `.env` has.
    """
    ok, _ = available()
    if not ok:
        return None
    import keyring

    try:
        return keyring.get_password(SERVICE, name) or None
    except Exception as exc:
        log.debug("keyring lookup for %s failed: %s", name, type(exc).__name__)
        return None


def put(name: str, value: str) -> None:
    """Store a value. Unlike `get`, this raises — the caller is a human."""
    ok, detail = available()
    if not ok:
        raise RuntimeError(f"no keychain available: {detail}")
    import keyring

    keyring.set_password(SERVICE, name, value)


def delete(name: str) -> bool:
    """Remove a value. False if there was nothing stored."""
    ok, detail = available()
    if not ok:
        raise RuntimeError(f"no keychain available: {detail}")
    import keyring

    try:
        keyring.delete_password(SERVICE, name)
        return True
    except Exception:
        return False


def load_into_env() -> list[str]:
    """Fill in secrets nothing else supplied. Returns the names taken.

    Deliberately lazy: if the environment and `.env` already cover everything,
    the keychain is never touched, so an install that does not use it pays
    nothing and is never prompted to unlock anything.
    """
    missing = [name for name in SECRETS if not os.environ.get(name)]
    if not missing or not available()[0]:
        return []

    taken = []
    for name in missing:
        value = get(name)
        if value:
            os.environ[name] = value
            _injected.add(name)
            taken.append(name)
    return taken


def dotenv_values(path: str | Path = ".env") -> dict[str, str]:
    """The secrets a `.env` file sets, with their values. Read, never written."""
    p = Path(path)
    if not p.is_file():
        return {}
    found = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key in SECRETS and value:
            found[key] = value
    return found


@dataclass
class Resolution:
    """Where one secret is coming from, and where else a copy exists."""

    name: str
    source: str = ""
    shadowed: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.source)


def inspect(dotenv_path: str | Path = ".env") -> list[Resolution]:
    """Locate every secret without reading any of them out.

    The environment already contains whatever `.env` put there, so telling the
    two apart means comparing against the file — a value that matches the file
    is attributed to the file.
    """
    file_values = dotenv_values(dotenv_path)
    keyring_ok = available()[0]

    resolutions = []
    for name in SECRETS:
        env_value = os.environ.get(name, "")
        file_value = file_values.get(name, "")

        places = []
        if env_value and env_value != file_value and name not in _injected:
            places.append(ENVIRONMENT)
        if file_value:
            places.append(DOTENV)
        if keyring_ok and get(name):
            places.append(KEYRING)

        resolutions.append(
            Resolution(name, places[0] if places else "", places[1:])
        )
    return resolutions
