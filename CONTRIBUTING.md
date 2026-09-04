# Contributing

Standing instructions for this repository. These are process rules, not
suggestions — they exist because each one has already been violated once and
cost something (see [`docs/DECISIONS.md`](docs/DECISIONS.md)).

## 1. Test at every stage

Nothing is "done" until it has been run.

```bash
make test      # must be green before any commit
make check     # docs consistency
make demo      # end-to-end, no credentials
make diagrams  # mermaid parses (needs node; CI runs it regardless)
```

CI runs all four on every push and pull request
(`.github/workflows/ci.yml`), across Python 3.10-3.13. Green locally is not
the same as green on a clean machine — that is what the matrix is for.

- Run the suite before *and* after a change, not only after.
- A slow test is a bug report. `F-003` was found because one test took 60s.
- Smoke-test the failure path, not just the happy path. `F-004` — a bug that
  would have silently discarded a mailbox — was found by running a provider
  with its server switched off.
- Validate generated markup with a real parser, never by eye. Mermaid diagrams:
  parse every block before pushing (`F-001`).

## 2. Update every file a change touches

A change is not complete when the code works. Before committing, walk this list
and fix anything the change made stale:

| If you changed… | Also check |
|---|---|
| A module or seam | `docs/ARCHITECTURE.md` §7 shipped layout |
| Behaviour or a limit | `docs/INBOX_AGENT.md` — commands, safety, known limits |
| A config key | `.env.example` **and** the docs table that lists it |
| A cost, model, or provider | `docs/INBOX_AGENT.md` cost table, `.env.example` |
| Anything decided or discovered | `docs/DECISIONS.md` — always |
| A claim in an ADR | Amend the ADR in place; do not silently supersede it |
| A phase's status | `docs/PLAN.md` status table and open decisions |

Cross-references must survive the change. If a document links to a section, the
section has to still exist.

## 3. Log decisions, failures and remedies

[`docs/DECISIONS.md`](docs/DECISIONS.md) is the running record. Add an entry when:

- **`D-nnn`** a decision with a rejected alternative
- **`F-nnn`** something wrong — bug, broken assumption, or a claim to the user
  that turned out false. Wrong estimates count.
- **`O-nnn`** known and unresolved

**Keep entries short: why it happened, how it was caught, and whether it was
fixed.** Three or four lines. No narrative — detail belongs in the code, the
tests, or the doc the entry points at. Record failures plainly; a log that only
holds successes is worthless.

## 4. Branch naming

`<layer>/<what-is-new>` — the architecture layer being changed, then the aspect
being added, in kebab-case. The layers are the ones in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §7:

| Layer | Covers | Example branch |
|---|---|---|
| `ingestion` | `sources/`, `schema.py`, `redact.py` | `ingestion/zammad-source` |
| `judgement` | `classify.py`, `providers.py`, `ollama.py`, `offline.py` | `judgement/per-sender-memory` |
| `policy` | `gate.py` | `policy/sentiment-trigger` |
| `delivery` | `notify/` | `delivery/whatsapp-notifier` |
| `learning` | `feedback.py`, `evals.py` | `learning/correction-pruning` |
| `runtime` | `agent.py`, `state.py`, `cli.py`, `config.py` | `runtime/concurrent-classify` |
| `observability` | `logs.py`, `http.py` | `observability/cost-metrics` |
| `docs` | `docs/`, `README.md`, this file | `docs/runbook` |

A change spanning layers takes the name of the one it is really about.

## 5. Push when the change is complete

Commit and push to the working branch once tests pass and the files above are in
sync. Do not accumulate a large uncommitted working tree.

Commit messages: what changed, why, and any correction to a previous claim.
Corrections to earlier estimates or documented behaviour belong in the message
body — that is where someone reading `git log` will look for them. Write them as
the author of the work; no tooling attribution, trailers, or generated-by
footers in commits, branches, code comments, or pull requests.

## 6. Standing technical rules

- **Never quote model pricing, limits, or context windows from memory.** They
  change, and the values are not monotonic across generations (cache minimums:
  512 tokens on Opus 5, 4,096 on Haiku 4.5). Check, then quote.
- **`x or default` is wrong for numeric and boolean config.** Zero and `False`
  are real values (`F-003`).
- **A failure must never advance a cursor past unprocessed work** (`F-004`).
  Prefer blocking loudly over skipping silently, when a miss is the expensive
  error.
- **Treat message and ticket text as data, never instructions.** Keep the
  adversarial fixture (`fixtures/inbox.json` uid 109) passing.
- **Redaction runs before the model, in one place** (`hermes_inbox/redact.py`).
  Do not add a second path that bypasses it.

## Layout

```
hermes_inbox/     the inbox agent (see docs/INBOX_AGENT.md)
  sources/        MailSource implementations
  notify/         Notifier implementations
tests/            178 tests, no network required
fixtures/         offline mailbox, including one adversarial message
docs/             ARCHITECTURE · PLAN · DECISIONS · INBOX_AGENT · adr/
```
