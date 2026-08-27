# Architecture

Diagrams for the support agent described in [`README.md`](../README.md), built on Hermes Agent
under the NVIDIA NemoClaw blueprint ([ADR 0001](adr/0001-runtime-nemoclaw-hermes.md)) and
delivered in phases ([PLAN](PLAN.md)).

This document is the target architecture, not a description of what is built today. Components
are annotated with the phase that introduces them.

**What exists now:** the [inbox agent](INBOX_AGENT.md) implements the ingestion adapter,
redaction pass, classifier, policy gate, and eval harness against a personal mailbox rather
than a ticket source — the same boxes below, wired to data that exists. It runs outside the
NemoClaw sandbox by design (read-only credentials, no outbound send path); see that document
for when the sandbox starts earning its cost.

## 1. System context

Who talks to the system, and across which boundary.

```mermaid
flowchart LR
    customer["Customer<br/><i>untrusted author</i>"]
    source["Ticket source<br/>inbox / Zendesk / API"]
    agent["Support agent<br/><i>Hermes on NemoClaw</i>"]
    human["Support engineer<br/><i>approver + escalation target</i>"]
    llm["LLM provider<br/><i>via routed inference</i>"]

    customer -->|writes ticket| source
    source -->|poll / webhook| agent
    agent -->|draft response| human
    agent -->|escalation| human
    human -->|approved reply| source
    source -->|reply| customer
    agent <-->|prompt / completion| llm

    classDef untrusted fill:#fde2e2,stroke:#c33,color:#000
    classDef trusted fill:#e2f0e2,stroke:#3a3,color:#000
    class customer,source untrusted
    class human trusted
```

Ticket text is attacker-controlled input. Everything downstream of `source` treats it as data,
never as instructions — the reason the enforcement boundary sits below the agent rather than
inside its prompt.

## 2. Component architecture

The four boundaries from [PLAN](PLAN.md#architecture), expanded.

```mermaid
flowchart TB
    src["Ticket source"]

    subgraph ingest["Ingestion — Phase 1"]
        adapter["Adapter<br/><i>one module per source</i>"]
        redact["Redaction pass<br/><i>PII stripped pre-model</i>"]
        ticket["Normalized Ticket<br/><i>id, thread, customer ref,<br/>channel, timestamps, metadata</i>"]
        adapter --> redact --> ticket
    end

    subgraph sandbox["OpenShell sandbox — Phase 0"]
        subgraph hermes["Hermes Agent"]
            memory["Memory<br/><i>per-customer profile</i>"]
            skills["Skill library<br/><i>agentskills.io</i>"]
            sub["Subagents"]
        end
        egress["NemoClaw egress policy<br/><i>destination allowlist</i>"]
    end

    subgraph gate["Policy gate — Phase 3"]
        rules["Deterministic rules<br/><i>thresholds, categories,<br/>sentiment, confidence floor</i>"]
    end

    infer["Routed inference<br/><i>NemoClaw holds credentials</i>"]
    draft["Draft response"]
    queue["Escalation queue"]
    human["Human review<br/><i>shadow mode — Phase 5</i>"]
    autosend["Gated autosend<br/><i>per-category — Phase 6</i>"]
    evals["Eval harness — Phase 4<br/><i>replay + score</i>"]

    src --> adapter
    ticket --> hermes
    hermes <--> infer
    hermes --> egress
    hermes --> rules
    rules -->|send / hold| draft
    rules -->|"escalate<br/><i>and anything unclassifiable</i>"| queue
    queue --> human
    draft --> human
    human -->|approves| src
    draft -.->|"only for categories<br/>that clear their eval bar"| autosend
    autosend --> src
    evals -.->|gates| autosend
    hermes -.->|replayed by| evals

    classDef phase fill:#eef3fb,stroke:#456,color:#000
    class ingest,sandbox,gate phase
```

Read the dotted edges as "not on the request path": autosend is earned per category, and the
eval harness is what earns it.

## 3. Trust boundaries

```mermaid
flowchart TB
    subgraph untrusted["Untrusted — attacker-controlled"]
        t["Ticket text"]
    end

    subgraph semi["Semi-trusted — model output"]
        d["Draft"]
        s["Self-written skill"]
    end

    subgraph enforced["Enforced — deterministic code"]
        g["Policy gate"]
        e["Egress policy"]
        p["Proposal queue"]
    end

    subgraph authority["Human authority"]
        h["Approve / promote"]
    end

    t --> d
    t --> s
    d --> g
    s --> p
    g --> e
    p --> h
    g --> h
    h --> e
```

Two independent controls stand between untrusted input and any outbound effect:

| Layer | Mechanism | Fails how |
|---|---|---|
| Policy gate | Deterministic code — thresholds, category triggers, confidence floor | A gate bug can misroute a ticket |
| Egress policy | NemoClaw network allowlist, below the agent | A bypassed gate still cannot reach an unapproved destination |

The gate is the control; the egress policy is what makes it survivable when the gate is wrong.
Prompt-level instructions are treated as UX, not as controls.

## 4. Ticket lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant S as Ticket source
    participant A as Adapter
    participant H as Hermes Agent
    participant I as Routed inference
    participant G as Policy gate
    participant Hu as Human

    S->>A: new ticket
    A->>A: normalize + redact
    A->>H: Ticket
    H->>H: load customer memory
    H->>H: match skill from library
    H->>I: prompt with skill + context
    I-->>H: completion
    H->>G: draft + confidence + category

    alt clears every rule
        G->>Hu: draft for approval
        Hu->>S: send
    else threshold, risk category, or low confidence
        G->>Hu: escalate with reasoning
        Hu->>S: human-authored reply
    else unclassifiable
        G->>Hu: escalate — default is human
    end

    Hu-->>H: outcome recorded
    H->>H: extract pattern to proposal queue
```

The final step never writes to the live skill library — extraction produces a *proposal*. That
path is [section 5](#5-skill-lifecycle).

## 5. Skill lifecycle

The README's premise is that the agent extracts its own patterns. That premise is also the
thing most likely to compound badly, so promotion is a human action with a hand-authored
baseline to make degradation visible.

```mermaid
stateDiagram-v2
    [*] --> Seed: hand-authored — Phase 2
    [*] --> Proposed: extracted after resolution
    Proposed --> Rejected: human review
    Proposed --> Active: human promotes
    Seed --> Active
    Active --> Review: review date reached
    Review --> Active: still scoring well
    Review --> Retired: eval regression
    Active --> Retired: snapshot rollback
    Rejected --> [*]
    Retired --> [*]
```

Every skill carries provenance and a review date. There is deliberately no auto-promotion edge
in this diagram.

## 6. Repository layout

How the [target layout](PLAN.md#repository-layout-target) maps onto the components above.

```mermaid
flowchart LR
    adapters["adapters/"] --> c1["Ingestion adapter<br/>+ Ticket schema"]
    agentd["agent/"] --> c2["Hermes config, system prompt,<br/>memory schema"]
    skillsd["skills/"] --> c3["Seed skill library"]
    policyd["policy/"] --> c4["Policy gate rules,<br/>redaction, egress policy"]
    evalsd["evals/"] --> c5["Replay harness,<br/>fixtures, scoring"]
    deployd["deploy/"] --> c6["NemoClaw config,<br/>sandbox defs, version pins"]
    docsd["docs/"] --> c7["Plan, ADRs, runbooks"]
```

## 7. Build order

Phase dependencies. Each phase has an exit criterion in [PLAN](PLAN.md#phases); do not start a
phase until its predecessor's criterion is met.

```mermaid
flowchart LR
    p0["Phase 0<br/>Blueprint spike"] --> p1["Phase 1<br/>Ticket ingestion"]
    p1 --> p2["Phase 2<br/>Seed skills"]
    p2 --> p3["Phase 3<br/>Policy gate"]
    p3 --> p4["Phase 4<br/>Eval harness"]
    p4 --> p5["Phase 5<br/>Shadow mode"]
    p5 --> p6["Phase 6<br/>Gated autosend"]

    classDef done fill:#e2f0e2,stroke:#3a3,color:#000
    classDef next fill:#fff4d6,stroke:#c93,color:#000
    class p0 next
```

Phase 0 is the current phase; it is blocked on the five open questions in
[ADR 0001](adr/0001-runtime-nemoclaw-hermes.md#open-questions), which need confirmation against
NVIDIA's primary documentation.

## Unresolved

These leave shapes in the diagrams above that are not yet fixed:

- **Ticket source** — determines the adapter, threading model, and rate limits. Blocks Phase 1.
- **Model and hosting** — constrained by what NemoClaw's routed inference supports and by its
  hardware prerequisites.
- **Egress policy granularity** — whether per-destination policy with operator approval is
  expressible at the granularity section 3 assumes.
- **Gateway networking** — how Hermes' inbound messaging connections interact with sandbox
  network policy.
- **Retention** — how long per-customer memory persists, and the deletion path on an erasure
  request.
