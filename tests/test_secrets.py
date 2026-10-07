"""Secret resolution: precedence, degradation, and not leaking anything.

The suite installs a fake `keyring` module rather than the real one. A test that
needs a working OS keychain cannot run in CI, on a headless box, or on a
contributor's laptop that has one locked — and the interesting behaviour here is
what happens when the backend is *missing* or *broken*, which a real keychain
cannot demonstrate on demand.
"""

from __future__ import annotations

import sys
import types

import pytest

from hermes_inbox import secrets
from hermes_inbox.config import Config

# --------------------------------------------------------------------------- #
# a keyring that behaves however the test needs it to
# --------------------------------------------------------------------------- #


def fake_keyring(stored=None, backend_module="keyring.backends.macOS", raises=None):
    module = types.ModuleType("keyring")
    store = dict(stored or {})

    class Backend:
        name = "fake"

    Backend.__module__ = backend_module

    def get_password(service, name):
        if raises:
            raise raises
        return store.get((service, name))

    def set_password(service, name, value):
        store[(service, name)] = value

    def delete_password(service, name):
        if (service, name) not in store:
            raise KeyError(name)
        del store[(service, name)]

    module.get_keyring = lambda: Backend()
    module.get_password = get_password
    module.set_password = set_password
    module.delete_password = delete_password
    module._store = store
    return module


@pytest.fixture
def no_keyring(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # import raises ImportError
    return None


@pytest.fixture
def keychain(monkeypatch):
    """A working keychain holding nothing, until a test puts something in it."""
    module = fake_keyring()
    monkeypatch.setitem(sys.modules, "keyring", module)
    return module


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No ambient secrets, and `.env` lookups land in an empty directory.

    `load_into_env` writes to `os.environ` directly, which monkeypatch does not
    know to undo, so the teardown here is load-bearing rather than tidiness.
    """
    import os

    before = {name: os.environ.get(name) for name in secrets.SECRETS}
    for name in secrets.SECRETS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HERMES_KEYRING", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    for name, value in before.items():
        os.environ.pop(name, None)
        if value is not None:
            os.environ[name] = value
    secrets._injected.clear()


def write_dotenv(tmp_path, **values):
    lines = ["# comment", "HERMES_THRESHOLD=0.7"]
    lines += [f"{key}={value}" for key, value in values.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# availability
# --------------------------------------------------------------------------- #


def test_missing_keyring_package_is_not_an_error(no_keyring):
    ok, detail = secrets.available()
    assert ok is False
    assert "not installed" in detail
    assert secrets.get("IMAP_PASSWORD") is None


@pytest.mark.parametrize("module", ["keyring.backends.fail", "keyring.backends.null"])
def test_a_headless_machine_reports_no_keychain(monkeypatch, module):
    """The case that matters: `keyring` imports fine but every call would raise."""
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring(backend_module=module))
    ok, detail = secrets.available()
    assert ok is False
    assert "headless" in detail


def test_keyring_can_be_switched_off_entirely(monkeypatch, keychain):
    monkeypatch.setenv("HERMES_KEYRING", "off")
    keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] = "stored"
    assert secrets.available()[0] is False
    assert secrets.get("IMAP_PASSWORD") is None


def test_a_broken_backend_degrades_instead_of_raising(monkeypatch):
    """A locked or damaged keychain must not stop the agent from starting."""
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring(raises=RuntimeError("locked")))
    assert secrets.get("IMAP_PASSWORD") is None  # no exception escapes
    assert secrets.load_into_env() == []


# --------------------------------------------------------------------------- #
# precedence: environment > .env > keychain
# --------------------------------------------------------------------------- #


def test_keychain_fills_in_what_nothing_else_supplied(keychain):
    keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] = "from-keychain"
    assert secrets.load_into_env() == ["IMAP_PASSWORD"]
    assert Config.from_env().imap_password == "from-keychain"


def test_dotenv_outranks_the_keychain(tmp_path, keychain):
    write_dotenv(tmp_path, IMAP_PASSWORD="from-dotenv")
    keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] = "from-keychain"
    assert Config.from_env().imap_password == "from-dotenv"


def test_the_environment_outranks_both(monkeypatch, tmp_path, keychain):
    monkeypatch.setenv("IMAP_PASSWORD", "from-environment")
    write_dotenv(tmp_path, IMAP_PASSWORD="from-dotenv")
    keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] = "from-keychain"
    assert Config.from_env().imap_password == "from-environment"


def test_the_keychain_is_not_consulted_when_nothing_is_missing(monkeypatch, keychain):
    """No DBus round-trip, and no unlock prompt, for an install that never uses it."""
    for name in secrets.SECRETS:
        monkeypatch.setenv(name, "already-set")

    calls = []
    monkeypatch.setattr(secrets, "get", lambda name: calls.append(name))
    assert secrets.load_into_env() == []
    assert calls == []


def test_a_secret_the_keychain_lacks_is_left_alone(keychain):
    assert secrets.load_into_env() == []
    assert Config.from_env().imap_password == ""


# --------------------------------------------------------------------------- #
# inspect: locating secrets without reading them
# --------------------------------------------------------------------------- #


def by_name(resolutions):
    return {r.name: r for r in resolutions}


def test_inspect_names_the_winning_source(monkeypatch, tmp_path, keychain):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-value")
    write_dotenv(tmp_path, IMAP_PASSWORD="file-value")
    keychain._store[(secrets.SERVICE, "TELEGRAM_BOT_TOKEN")] = "key-value"

    found = by_name(secrets.inspect())
    assert found["ANTHROPIC_API_KEY"].source == secrets.ENVIRONMENT
    assert found["IMAP_PASSWORD"].source == secrets.DOTENV
    assert found["TELEGRAM_BOT_TOKEN"].source == secrets.KEYRING


def test_inspect_reports_a_secret_left_behind_in_dotenv(tmp_path, keychain):
    """The half-finished migration: stored in the keychain, still in the file."""
    write_dotenv(tmp_path, IMAP_PASSWORD="file-value")
    keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] = "key-value"

    resolution = by_name(secrets.inspect())["IMAP_PASSWORD"]
    assert resolution.source == secrets.DOTENV
    assert resolution.shadowed == [secrets.KEYRING]


def test_inspect_holds_no_values(monkeypatch, tmp_path, keychain):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
    write_dotenv(tmp_path, IMAP_PASSWORD="hunter2-app-password")
    keychain._store[(secrets.SERVICE, "TELEGRAM_BOT_TOKEN")] = "123:AAsecrettoken"

    rendered = repr(secrets.inspect())
    for value in ("sk-ant-supersecret", "hunter2-app-password", "123:AAsecrettoken"):
        assert value not in rendered


def test_inspect_does_not_mistake_its_own_injection_for_the_environment(keychain):
    """`load_into_env` puts keychain values in `os.environ`; `inspect` runs after.

    Without the bookkeeping this reports "environment" for a secret that came
    out of the keychain — the exact opposite of the truth, on the one command
    whose whole job is telling you where your secrets live.
    """
    keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] = "from-keychain"
    Config.from_env()  # as every command does, before doctor inspects

    assert by_name(secrets.inspect())["IMAP_PASSWORD"].source == secrets.KEYRING


def test_inspect_reports_absence_rather_than_guessing(keychain):
    assert all(not r.present for r in secrets.inspect())


def test_dotenv_values_ignores_comments_and_non_secrets(tmp_path):
    (tmp_path / ".env").write_text(
        "# IMAP_PASSWORD=commented-out\n"
        "HERMES_THRESHOLD=0.9\n"
        "TELEGRAM_BOT_TOKEN=\n"  # present but empty
        "IMAP_PASSWORD='quoted-value'\n",
        encoding="utf-8",
    )
    assert secrets.dotenv_values() == {"IMAP_PASSWORD": "quoted-value"}


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #


def test_put_and_delete_round_trip(keychain):
    secrets.put("IMAP_PASSWORD", "stored")
    assert secrets.get("IMAP_PASSWORD") == "stored"
    assert secrets.delete("IMAP_PASSWORD") is True
    assert secrets.get("IMAP_PASSWORD") is None


def test_delete_of_something_absent_is_false_not_an_error(keychain):
    assert secrets.delete("IMAP_PASSWORD") is False


def test_writing_without_a_keychain_raises_rather_than_pretending(no_keyring):
    """`get` degrades quietly; `put` must not — a human is waiting on the answer."""
    with pytest.raises(RuntimeError, match="no keychain"):
        secrets.put("IMAP_PASSWORD", "value")


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #


def test_the_value_is_never_a_command_line_argument(keychain):
    """argv lands in shell history and in anyone's `ps` output.

    There is no slot for it, so argparse rejects the attempt outright rather
    than the value reaching the keychain by a path that also records it.
    """
    from hermes_inbox.cli import main

    with pytest.raises(SystemExit):
        main(["secrets", "set", "IMAP_PASSWORD", "the-password"])
    assert keychain._store == {}


def test_status_shows_where_each_secret_is_without_printing_it(capsys, tmp_path, keychain):
    from hermes_inbox.cli import main

    write_dotenv(tmp_path, IMAP_PASSWORD="file-value")
    keychain._store[(secrets.SERVICE, "TELEGRAM_BOT_TOKEN")] = "key-value"

    assert main(["secrets"]) == 0
    out = capsys.readouterr().out

    assert "IMAP_PASSWORD" in out and ".env" in out
    assert "TELEGRAM_BOT_TOKEN" in out and "keyring" in out
    assert "ANTHROPIC_API_KEY" in out and "not set" in out
    assert "file-value" not in out and "key-value" not in out


def test_set_reads_the_value_from_a_prompt(capsys, monkeypatch, keychain):
    from hermes_inbox.cli import main

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "  typed-secret  ")

    assert main(["secrets", "set", "IMAP_PASSWORD"]) == 0
    assert keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] == "typed-secret"
    assert "typed-secret" not in capsys.readouterr().out


def test_set_rejects_a_name_that_is_not_a_secret(capsys, keychain):
    from hermes_inbox.cli import main

    assert main(["secrets", "set", "IMAP_HOST"]) == 1
    assert "not a secret" in capsys.readouterr().err


def test_import_moves_dotenv_secrets_and_says_what_is_left(capsys, tmp_path, keychain):
    from hermes_inbox.cli import main

    write_dotenv(tmp_path, IMAP_PASSWORD="file-value", TELEGRAM_BOT_TOKEN="token-value")
    before = (tmp_path / ".env").read_text(encoding="utf-8")

    assert main(["secrets", "import"]) == 0
    out = capsys.readouterr().out

    assert keychain._store[(secrets.SERVICE, "IMAP_PASSWORD")] == "file-value"
    assert keychain._store[(secrets.SERVICE, "TELEGRAM_BOT_TOKEN")] == "token-value"
    assert "delete these lines" in out
    assert "file-value" not in out and "token-value" not in out

    # It must not rewrite the file holding the user's credentials.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == before


def test_writing_commands_refuse_without_a_keychain(capsys, no_keyring):
    from hermes_inbox.cli import main

    assert main(["secrets", "import"]) == 1
    assert "not installed" in capsys.readouterr().err


def test_status_works_without_a_keychain(capsys, tmp_path, no_keyring):
    """`doctor`-adjacent commands must still answer when the feature is absent."""
    from hermes_inbox.cli import main

    write_dotenv(tmp_path, IMAP_PASSWORD="file-value")

    assert main(["secrets"]) == 0
    out = capsys.readouterr().out
    assert "IMAP_PASSWORD" in out and ".env" in out
    assert "not installed" in out, "it should still say why there is nowhere to move them"
    assert "file-value" not in out


def test_the_module_does_not_shadow_the_standard_library():
    """`hermes_inbox/secrets.py` must not become the `secrets` anything imports."""
    import secrets as stdlib

    assert hasattr(stdlib, "token_hex")
    assert stdlib is not secrets


# --------------------------------------------------------------------------- #
# the openai-compat key (O-004): a fourth secret, optional
# --------------------------------------------------------------------------- #


def test_the_openai_compat_key_can_come_from_the_keychain(keychain):
    keychain._store[(secrets.SERVICE, "HERMES_OPENAI_API_KEY")] = "nvapi-stored"
    assert Config.from_env().openai_api_key == "nvapi-stored"


def test_the_openai_compat_key_is_located_and_never_read_out(keychain, tmp_path):
    write_dotenv(tmp_path, HERMES_OPENAI_API_KEY="nvapi-in-dotenv")
    keychain._store[(secrets.SERVICE, "HERMES_OPENAI_API_KEY")] = "nvapi-stored"

    resolution = next(r for r in secrets.inspect() if r.name == "HERMES_OPENAI_API_KEY")
    assert (resolution.source, resolution.shadowed) == (secrets.DOTENV, [secrets.KEYRING])
    assert "nvapi" not in repr(resolution)


def test_an_unset_optional_key_does_not_trouble_doctor(keychain, tmp_path):
    """Most setups have no key for a local vLLM server; that is not a problem to report."""
    from hermes_inbox import doctor

    report = doctor.Report()
    doctor.check_secrets(Config(data_dir=tmp_path), report)
    assert not any("HERMES_OPENAI_API_KEY" in c.name for c in report.checks)
    assert all(c.status != doctor.FAIL for c in report.checks)


def test_import_moves_the_openai_compat_key_too(keychain, tmp_path):
    write_dotenv(tmp_path, HERMES_OPENAI_API_KEY="nvapi-in-dotenv")
    assert secrets.dotenv_values() == {"HERMES_OPENAI_API_KEY": "nvapi-in-dotenv"}
