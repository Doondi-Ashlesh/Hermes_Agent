# Hermes Agent

A self-improving triage agent. It watches a stream of incoming messages, decides
which ones need a human, acts, and gets better at *your* judgement calls as you
correct it.

The source it reads and the action it takes are both pluggable. What sits between
them — normalization, redaction, judgement, a deterministic policy gate, a
correction loop, and an evaluation harness — is the actual product.

```
   source          judge            gate              action         learn
   ──────          ─────            ────              ──────         ─────
   mailbox   →   importance   →   deterministic   →   notify    →   corrections
   tickets       or a draft       rules, not a        or send       replayed and
   anything                       model's opinion                   scored
       ↑                                                                │
       └────────────────────────────────────────────────────────────────┘
```

Roughly two-thirds of the code doesn't know or care what it's reading.

## Deployments

### 1. Inbox agent — running today

Watches a mailbox and pings you on Telegram only when something actually needs
you. Press a button when it gets a call wrong, and that correction goes into
every later judgement.

```bash
make install && make demo    # 12 fixture emails, no credentials needed
```

Real mailbox in about 20 minutes: **[SETUP.md](docs/SETUP.md)**.

Read-only by construction — the mailbox is opened `readonly=True` and fetched
with `BODY.PEEK[]`, and there is no send path in the codebase. It cannot alter
your mail.

### 2. Support agent — planned

The same machinery pointed at a ticket source, drafting replies instead of
judging importance, with the gate deciding send / hold / escalate instead of
notify / stay silent.

It is not built, and the honest reason is that it needs two things that do not
exist yet: a corpus of real tickets, and a write scope. The write scope is what
finally makes the NemoClaw sandbox load-bearing
([ADR 0001](docs/adr/0001-runtime-nemoclaw-hermes.md)).

| | Inbox agent | Support agent |
|---|---|---|
| Source | IMAP mailbox | Ticket system |
| Judgement | How important is this? | What is the reply? |
| Gate decides | Notify / stay silent | Send / hold / escalate |
| Human role | Corrects the call | Approves the draft |
| Needs a write scope | No | **Yes** |
| Status | **Running** | Planned |

## How it improves

There is no fine-tuning. Corrections are appended as labeled examples and
rendered into the prompt above whatever is being judged, marked as outranking
the standing guidance. It stops making a mistake because you told it not to, and
the telling persists.

The half that makes this honest is the eval harness. Corrections can conflict,
overfit, or drown each other out, so every stored correction is replayed with
**itself excluded from the prompt** and scored. Without that, "it's getting
better" is unfalsifiable.

```bash
make eval
```

Watch recall. A false positive is one unwanted interruption; a false negative is
something important you never saw.

## Project docs

| | |
|---|---|
| [Setup](docs/SETUP.md) | Clean machine to running agent, with a verify step at each stage |
| [Inbox agent](docs/INBOX_AGENT.md) | What deployment 1 does, its commands and limits |
| [Extending](docs/EXTENDING.md) | One message traced through the code, plus recipes for new sources, notifiers, providers and gate rules |
| [Architecture](docs/ARCHITECTURE.md) | Diagrams: system context, components, trust boundaries, lifecycles |
| [Plan](docs/PLAN.md) | The phase model both deployments earn their autonomy through |
| [Decisions](docs/DECISIONS.md) | Why it is built this way, what broke, what was done about it |
| [ADR 0001](docs/adr/0001-runtime-nemoclaw-hermes.md) | Why Hermes runs under the NVIDIA NemoClaw blueprint |
| [CLAUDE.md](CLAUDE.md) | Working agreement: test, sync docs, log, push |

## Status

Deployment 1 runs. 132 tests, no network or credentials required.

Not yet done, and deliberately so: reply drafting, the NemoClaw sandbox, and
validation against a real mailbox over a meaningful period. The open items are
tracked at the end of [DECISIONS.md](docs/DECISIONS.md).
