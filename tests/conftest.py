"""Suite-wide guarantees.

The suite runs with no network and no credentials. `HERMES_KEYRING=off` extends
that to the OS keychain: without it, the same `make test` that passes silently in
CI — where `keyring` is not even installed — would reach into a real Keychain on
a contributor's laptop, and could sit there waiting on an unlock dialog.

`tests/test_secrets.py` clears the switch for its own tests and supplies a fake
`keyring` module instead, so the behaviour is still covered, just never against
whatever the host happens to have.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def never_touch_a_real_keychain(monkeypatch):
    monkeypatch.setenv("HERMES_KEYRING", "off")
