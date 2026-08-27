# Implementation Plan

Hermes Agent under the NVIDIA NemoClaw blueprint. Runtime rationale and trade-offs:
[ADR 0001](adr/0001-runtime-nemoclaw-hermes.md).

## Two deployments, one phase model

The project ships one piece of machinery with two deployments
([README](../README.md)). Both earn autonomy the same way — the phases below are a
trust ladder, not a build order for a single product.

| | Track A — inbox agent | Track B — support agent |
|---|---|---|
| Source | IMAP mailbox | Ticket system |
| Judgement | Importance | A drafted reply |
| Gate decides | Notify / silent | Send / hold / escalate |
| Write scope | Not needed | **Required** |
| Sandbox | Not yet load-bearing ([D-005](DECISIONS.md#d-005--ship-outside-the-nemoclaw-sandbox-for-now)) | Load-bearing from day one |
| Status | **Running** | Not started |

## Status — 2026-08-27

**Track A** has the machinery built and running: ingestion, redaction, judgement,
policy gate, notification, correction loop, eval harness. What remains is
validation — every accuracy claim is against 12 fixtures until it has scored a real
mailbox ([O-003](DECISIONS.md#o-003--not-validated-against-real-mail)).

**Track B** has not started. It is blocked on two things, and neither is the runtime:
a corpus of real tickets, and the write scope that drafting requires. Phase 0 is
additionally waiting on three ADR questions — no longer on hardware
([F-006](DECISIONS.md#f-006--adr-0001-claimed-dgx-class-hardware-was-required)).

Because Track A shares the phase model, it has already cleared the substance of
several phases against its own source:

| Phase | Track B (tickets) | Track A (mail) |
|---|---|---|
| 0 — Blueprint spike | Not started | Deliberately skipped; no write scope to sandbox |
| 1 — Ingestion | Not started | ✅ `Message`, `sources/`, `redact.py` |
| 2 — Seed skills | Not started | ✅ Corrections are proposals; you promote by labeling |
| 3 — Policy gate | Not started | ✅ `gate.py`, adversarial fixture passing |
| 4 — Eval harness | Not started | ✅ `evals.py`, leave-one-out |
| 5 — Shadow mode | Not started | ✅ Read-only by construction — no send path to gate |
| 6 — Gated autosend | Not started | N/A — nothing to send |

Reasoning, corrections and known failures for both tracks: [DECISIONS.md](DECISIONS.md).

## Architecture

Rendered diagrams — system context, components, trust boundaries, ticket and skill lifecycles —
are in [ARCHITECTURE.md](ARCHITECTURE.md).

```
  ticket source (inbox / ticketing API)
            |
            v
  +---------------------+
  |  ingestion adapter  |   normalizes to internal Ticket schema
  +---------------------+
            |
            v
  +---------------------------------------------+
  |            OpenShell sandbox                 |
  |  +---------------------------------------+   |
  |  |          Hermes Agent                  |   |
  |  |  memory  |  skills  |  subagents       |   |
  |  +---------------------------------------+   |
  |         ^                    |               |
  |         |                    v               |
  |   routed inference     policy gate           |
  +---------------------------------------------+
            |                    |
            v                    v
      draft response        escalation queue (human)
            |
            v
   shadow mode: human approves  ->  autosend (gated, later)
```

Four boundaries matter, and they are deliberately separate:

1. **Ingestion adapter** — the only component that knows the ticket source's API. Everything
   downstream sees a normalized `Ticket`. Swapping Zendesk for email must not touch agent code.
2. **Sandbox (OpenShell)** — the execution boundary. Everything the agent does, including
   running its own self-written skills, happens inside it.
3. **Policy gate** — deterministic code, not prompt instructions. Decides send / hold /
   escalate. Backed by NemoClaw network egress policy so that a bypassed gate still cannot
   reach an unapproved destination.
4. **Human boundary** — shadow mode by default. Autosend is a per-category opt-in earned by
   evaluation results, never a global switch.

## Repository layout (target)

```
adapters/        ingestion adapters; one module per source, common Ticket schema
agent/           Hermes configuration, system prompt, memory schema
skills/          hand-authored seed skills (agentskills.io format)
policy/          escalation rules, redaction, egress policy definitions
evals/           replay harness, fixtures, scoring
deploy/          NemoClaw config, sandbox definitions, version pins
docs/            plan, ADRs, runbooks
```

## Phases

Each phase has an exit criterion. Do not start the next phase until it is met.

Written for Track B, since that is the track with autonomy to earn. Track A's
mapping is in the status table above; where a phase says "ticket", read "message".

### Phase 0 — Blueprint spike

> **Partially unblocked.** Two of ADR 0001's five questions are answered
> ([amendment](adr/0001-runtime-nemoclaw-hermes.md#amendment-2026-08-27)); Q3–Q5 remain and
> are answerable from NemoClaw's committed `docs/**/*.mdx`.

Resolve the open questions in ADR 0001 against NVIDIA's primary documentation, then stand up
the stock blueprint with no customization: NemoClaw installed, Hermes running under OpenShell,
inference routed, one message round-tripped through the gateway.

The point is operator familiarity before there is any project code to confuse it with. Record
what the stock install actually produces — config file locations, CLI surface, default network
policy — in `docs/runbook.md`.

**Exit:** a documented, reproducible install; ADR 0001's five open questions answered in
writing; version pins committed to `deploy/`.

### Phase 1 — Ticket ingestion

Pick one source and build for it. The choice determines schema, rate limits, threading model,
and auth, so it is the first real decision after the runtime.

Build the adapter against a **mock inbox** with fixture tickets. No production support data
touches the system in this phase.

Define the `Ticket` schema: id, thread history, customer ref, channel, timestamps, attachments,
metadata. Define the redaction pass that runs before any ticket text reaches the model.

**Exit:** fixture tickets flow from mock source to agent and produce a draft, entirely offline.

### Phase 2 — Seed skills

Hand-author skills for the top 5–10 ticket categories before enabling any self-improvement.
The README's premise is that the agent extracts its own patterns; that premise is the most
likely thing to compound badly, and a hand-built baseline is what makes degradation visible.

Self-extracted skills are written to a **proposal queue**, not the live library. Promotion is a
human action. Every skill carries provenance and a review date.

**Exit:** seed library covers the fixture set; proposal queue works; no auto-promotion path exists.

### Phase 3 — Policy gate

Implement escalation as code:

- Monetary thresholds (refunds, credits) — hard limits, not model judgment.
- Category triggers — legal, security, data deletion, chargebacks.
- Sentiment and repeat-contact triggers.
- Confidence floor — low-confidence drafts escalate rather than send.

Mirror each rule in NemoClaw egress policy wherever it has a network expression, so the
enforcement survives the gate being wrong. Any ticket the gate cannot classify escalates —
the default is human, always.

**Exit:** every fixture ticket routes correctly; adversarial fixtures (tickets that explicitly
instruct the agent to ignore its rules) escalate rather than comply.

### Phase 4 — Evaluation harness

Replay historical resolved tickets and diff the agent's draft against what the human actually
sent. Score on resolution correctness, escalation precision and recall, and tone.

This is both the gate for enabling autosend and the regression suite for every subsequent skill
change. It is worth more than any other single component in the project: without it, "the agent
is improving" is an unfalsifiable claim.

**Exit:** harness runs on demand and in CI; baseline scores recorded.

### Phase 5 — Shadow mode

Real tickets, real customer data, drafts only, human approves every response. First phase where
the sandbox and egress policy do load-bearing work.

Run until the eval scores are stable across a meaningful ticket volume and the escalation
false-negative rate is acceptable — a missed escalation is far more costly than an unnecessary one.

**Exit:** stable scores over an agreed window; no escalation false negatives in the
high-risk categories.

### Phase 6 — Gated autosend

Enable autosend for the **narrowest** category that clears its eval bar. One category at a time,
each with an independent kill switch and an automatic rollback trigger on score regression.

## Risks

| Risk | Mitigation |
|---|---|
| Prompt injection via ticket text | Egress policy below the agent; adversarial fixtures in evals; redaction before model input |
| Skill library degradation | Proposal queue, provenance, review dates, snapshot rollback, eval regression suite |
| Missed escalation | Unclassifiable defaults to human; false negatives gate Phase 5 exit |
| Customer PII scope | Redaction pass; memory scoped per customer; retention policy defined in Phase 1 |
| Two-system debugging | Phase 0 spike before project code exists; runbook records stock behavior |
| Blueprint drift | Pinned versions in `deploy/`; deliberate scheduled upgrades |

## Decisions still open

1. **Ticket source** — blocks Phase 1. Email, Zendesk, or something else. *(The inbox agent
   answers this for its own track: IMAP, see [D-002](DECISIONS.md#d-002--imap-before-the-gmail-api).)*
2. ~~**Model**~~ — **resolved 2026-08-27.** NemoClaw's routed inference supports Anthropic,
   OpenAI, Gemini, OpenRouter and NVIDIA endpoints, plus local serving via Ollama / llama.cpp /
   vLLM / NIM ([ADR 0001 amendment](adr/0001-runtime-nemoclaw-hermes.md#amendment-2026-08-27)).
   No longer a constraint; a preference.
3. ~~**Hosting**~~ — **resolved 2026-08-27.** The floor is 4 vCPU / 8 GB RAM / 20 GB disk with
   no local GPU required, not DGX-class as originally recorded
   ([F-006](DECISIONS.md#f-006--adr-0001-claimed-dgx-class-hardware-was-required)).
4. **Historical ticket access** — Phase 4 needs a corpus of resolved tickets; availability and
   volume are unknown. *(The inbox agent sidesteps this: the user's own sent mail is the
   corpus.)*
5. **Retention** — how long per-customer memory persists, and the deletion path when a customer
   requests erasure.
