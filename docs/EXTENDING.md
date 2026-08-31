# Extending

For an engineer who has run it ([SETUP.md](SETUP.md)) and now wants to change it.

## One email, end to end

Follow a single message through the code. Every file it touches, in order:

```
hermes_inbox/agent.py::Agent.cycle          ← entry point, one poll
│
├─ notifier.poll_feedback(offset)           notify/telegram.py
│    └─ button presses → feedback.jsonl     feedback.py::FeedbackStore.add
│
├─ source.fetch_new(since_uid)              sources/imap.py
│    └─ raw RFC822 → Message                schema.py::Message
│
├─ feedback.recent(max_examples)            feedback.py  ← your corrections
│
└─ for each Message:
     ├─ classify_fn(message, examples, …)   providers.py::resolve picked this
     │    ├─ redact_message(message)        redact.py     ← always, no bypass
     │    ├─ build_system(examples)         classify.py   ← cached prefix
     │    ├─ build_user(message)            classify.py   ← volatile part
     │    └─ → Verdict                      schema.py::Verdict
     │
     ├─ gate.decide(message, verdict, cfg)  gate.py       ← deterministic
     │    └─ → GateDecision(notify, rule)
     │
     ├─ log.append(Decision)                state.py::DecisionLog
     ├─ if notify: notifier.send(decision)  notify/telegram.py
     └─ state.set_last_uid(source, uid)     state.py::State
```

Three invariants worth knowing before you change anything:

1. **Redaction is inside the classifier**, not in the loop — so no new caller can
   skip it by forgetting.
2. **The cursor advances only after a successful classify.** A failure `break`s
   the cycle. Do not "fix" this by continuing; it is deliberate
   ([F-004](DECISIONS.md#f-004--a-provider-outage-would-have-discarded-the-mailbox)).
3. **The gate never consults the model except at the last rule.** Explicit human
   rules outrank the score by construction.

---

## Recipe: a new mail source

Implement one method. Gmail API instead of IMAP, as a worked example.

**1. Write it** — `hermes_inbox/sources/gmail.py`:

```python
from ..schema import Message


class GmailSource:
    name = "gmail"                      # used as the state key; must be stable

    def __init__(self, credentials):
        self.credentials = credentials

    def fetch_new(self, since_uid: str | None = None, limit: int = 25) -> list[Message]:
        """Oldest first. Must not mutate server state — no marking as read."""
        raw = self._list_since(since_uid, limit)
        return [
            Message(
                uid=item["id"],                 # must sort/compare consistently
                source=self.name,
                sender=item["from"].lower(),
                sender_name=item.get("from_name", ""),
                subject=item["subject"],
                body=item["body"],
                received_at=item["date"],       # timezone-aware datetime
                headers={"List-Unsubscribe": ...},   # optional; the gate reads these
            )
            for item in raw
        ]
```

**2. Register it** — `cli.py::_source`.

**3. Test it** against the same contract the others meet:

```python
def test_gmail_source_respects_since_uid():
    messages = GmailSource(fake_creds).fetch_new(since_uid="109")
    assert [m.uid for m in messages] == ["110", "111", "112"]
```

**Gotcha:** `uid` is the cursor. It must be monotonic and comparable the same way
`State.last_uid` stores it (a string). IMAP UIDs are integers-as-strings; Gmail
message ids are not ordered, so a Gmail source needs `historyId` or an internal
date as its cursor instead.

---

## Recipe: a new notifier

Two methods. WhatsApp, the one most likely to be wanted
([D-003](DECISIONS.md#d-003--telegram-before-whatsapp)).

**1. Write it** — `hermes_inbox/notify/whatsapp.py`:

```python
from ..schema import Decision


class WhatsAppNotifier:
    name = "whatsapp"

    def __init__(self, token: str, phone_id: str, recipient: str):
        ...

    def send(self, decision: Decision) -> None:
        message, verdict = decision.message, decision.verdict
        self._post("messages", {
            "to": self.recipient,
            "type": "template",          # business-initiated messages must be templates
            "template": {...},
        })

    def poll_feedback(self, offset=None) -> tuple[list[tuple[str, bool]], int | None]:
        """Return [(message_uid, is_important), ...] and the next offset.

        A channel that cannot carry a reply returns ([], offset) — never raises.
        """
        return [], offset
```

**2. Register it** — `cli.py::_notifier`, plus config in `config.py` and
`.env.example` (`make check` fails if you forget the latter).

**Gotchas:**

- `poll_feedback` returning labels for a uid with no logged decision is reported
  as an error, not silently dropped. The uid must be the one you sent.
- Errors in `send` are caught by the loop and recorded — triage continues. Do
  not swallow them yourself; let them propagate so they are logged.
- WhatsApp specifically: business-initiated messages need pre-approved templates,
  which is why Telegram shipped first. The seam is ready; the paperwork is not.

---

## Recipe: a new classifier provider

The signature is the contract — all three implementations match it exactly, and
a test asserts that.

```python
def classify(message, examples=None, config=None, client=None) -> Verdict:
```

**1. Write it** — `hermes_inbox/myprovider.py`:

```python
from .classify import SCHEMA, build_system, build_user
from .redact import redact_message
from .schema import Verdict


def classify(message, examples=None, config=None, client=None) -> Verdict:
    safe = redact_message(message)                      # never skip
    system = "\n\n".join(b["text"] for b in build_system(examples or []))
    raw = my_api(system=system, user=build_user(safe), schema=SCHEMA)

    # Clamp. Weak models return out-of-range scores and invented categories
    # even under a schema, and must not be able to crash the polling loop.
    raw["score"] = max(0.0, min(1.0, float(raw.get("score", 0.0))))
    if raw.get("category") not in SCHEMA["properties"]["category"]["enum"]:
        raw["category"] = "other"
    return Verdict.from_dict(raw)
```

**2. Register it** — add the name to `providers.py::NAMES`, a branch in
`resolve`, and a line in `describe`.

**3. Test it** with a stubbed transport — see `tests/test_providers.py`. Stub
the HTTP layer, never call a real endpoint in a test.

**4. Measure it** against the others on real corrections:

```bash
hermes-inbox eval --provider myprovider
```

That number, not the vendor's benchmark, is what decides whether it is good
enough for your mail.

---

## Recipe: a new gate rule

`gate.py::decide` is a flat, ordered sequence. Add a rule where its precedence
belongs and return a `GateDecision` naming itself:

```python
    if message.headers.get("List-Unsubscribe") and verdict.score < 0.9:
        return GateDecision(notify=False, rule="bulk-mail")
```

**Rules:**

- Every branch returns a `rule` string. `make stats` groups by it, so an unnamed
  rule is invisible when you are debugging why something did or didn't fire.
- Deterministic only — no model calls, no network, no clock beyond the injected
  `now`. `decide` takes `now` as a parameter precisely so time-based rules are
  testable.
- Order is the contract. A rule placed above `never-senders` overrides an
  explicit human mute, which is almost never right.

Test both directions — that it fires, and that it loses to a higher rule:

```python
def test_bulk_rule_loses_to_an_explicit_always_sender():
    cfg = GateConfig(threshold=0.5, always_senders=["boss@work.com"])
    msg = make_message(sender="boss@work.com", headers={"List-Unsubscribe": "<x>"})
    assert decide(msg, make_verdict(0.1), cfg).notify is True
```

---

## Changing the prompt

`classify.py` splits the prompt for caching
([D-009](DECISIONS.md#d-009--corrections-live-in-the-cached-system-prefix)):

| Function | Contents | Cached |
|---|---|---|
| `build_system` | Standing rules + your corrections | yes, breakpoint on the last block |
| `build_user` | The single message | no |

Anything stable goes in `build_system`. Anything per-message goes in
`build_user`. Putting a timestamp or per-message id in the system blocks
silently invalidates the cache on every call — the symptom is
`cache_read_input_tokens: 0` across repeated requests, with no error.

If you change `SCHEMA`, update `CATEGORIES` alongside it — `test_schema_is_closed`
asserts `required` covers every property, and the offline and Ollama providers
both validate against the same enum.

---

## Dev workflow

```bash
make test      # full suite, no network, <2s
make check     # docs consistency: links, anchors, env vars, CLI coverage
make demo      # end-to-end against fixtures
make diagrams  # parse every mermaid block (needs node)
```

All four run in CI on every push and PR, plus a Python 3.10-3.13 matrix and a
check that the adversarial fixture is still suppressed.

`make check` exists because docs drift silently. It fails the build if you add a
config key without documenting it, add a CLI command without a docs entry, break
a cross-reference, or let the cost table name a different default model than the
code does.

### Before you push

The checklist in [`CLAUDE.md`](../CLAUDE.md) is the short version:

1. `make test` green
2. `make check` green
3. Docs updated for what you touched — the table in `CLAUDE.md` §2 says which
4. An entry in [DECISIONS.md](DECISIONS.md) if you decided something or something broke

### Testing conventions

- **No network in tests.** Stub the transport (`tests/test_providers.py` stubs
  `urlopen`, `tests/test_classify.py` stubs the SDK client).
- **Test the failure path.** The two worst bugs in this repo's history were both
  found that way, not by testing success
  ([F-003](DECISIONS.md#f-003--interval-or-selfconfiginterval-treated-0-as-unset),
  [F-004](DECISIONS.md#f-004--a-provider-outage-would-have-discarded-the-mailbox)).
- **A slow test is a bug report.** The suite runs in under a second; if a change
  makes it take 60, something is sleeping when it shouldn't.
- **Keep the adversarial fixture passing.** `fixtures/inbox.json` uid 109 tries
  to instruct the classifier. It must stay classified as spam.

---

## Where things are

```
hermes_inbox/
  agent.py        run loop — fetch, classify, gate, notify, record
  schema.py       Message, Verdict, GateDecision, Decision
  config.py       env → Config; add new keys here and in .env.example
  redact.py       secret-stripping, single path, runs before every model call
  classify.py     Anthropic provider + the shared prompt builders
  ollama.py       local provider
  offline.py      keyword rules; no model, used by demo and CI
  providers.py    registry: name → classify_fn
  http.py         JSON-over-HTTP with retries for the non-SDK providers
  logs.py         logging setup: text or json, level from env
  gate.py         ordered deterministic rules
  feedback.py     labeled examples, the correction loop
  evals.py        leave-one-out replay scoring
  state.py        read cursor + append-only decision log
  cli.py          argparse entry point
  sources/        MailSource implementations (base, imap, fixtures)
  notify/         Notifier implementations (base, telegram, console)
scripts/
  check_links.py  doc link and anchor verification
tests/            158 tests, no network required
fixtures/         offline mailbox incl. one adversarial message
```
