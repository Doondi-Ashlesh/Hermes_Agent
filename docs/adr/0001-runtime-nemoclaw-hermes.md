# ADR 0001: Build the support agent on Hermes Agent under the NVIDIA NemoClaw blueprint

- **Status:** Accepted — amended 2026-08-27
- **Date:** 2026-08-24
- **Deciders:** Project owner
- **Amendments:** [2026-08-27](#amendment-2026-08-27) — Q1 and Q2 answered; the
  deployment-floor cost was overstated and has been corrected

## Context

The project is a self-improving customer support agent (see `README.md`). At the time of
this decision the repository contained only a product description — no code, no runtime
configuration, no ingestion path.

Two things make the runtime choice consequential rather than cosmetic:

1. **The agent processes untrusted inbound text.** Support tickets are written by
   strangers. An agent that reads tickets and also has shell, filesystem, and network
   access is a prompt-injection target with a direct path to customer data.
2. **The agent is designed to write its own skills.** Capabilities acquired at runtime
   cannot be fully reviewed in advance, so the boundary around what the agent *can* do
   matters more than in a system whose tool surface is fixed at build time.

Both point to the same requirement: the security boundary must sit below the agent, not
inside its prompt. Escalation rules expressed as instructions ("do not issue refunds above
$X") are advisory. A ticket author who can talk the model out of them faces no other
obstacle.

### Options considered

**A. Bare Hermes Agent.** Hermes (Nous Research, MIT) provides the pieces the product
description assumes: persistent memory, self-created skills on the agentskills.io standard,
subagents, and a messaging gateway spanning Telegram/Discord/Slack/WhatsApp/Signal. It runs
on modest hardware. It does not provide an enforcement boundary — guardrails are the
operator's problem.

**B. Hermes under NVIDIA NemoClaw.** [NemoClaw](https://github.com/NVIDIA/NemoClaw)
(Apache 2.0) is an open reference stack for running agents inside NVIDIA OpenShell
sandboxes. Hermes is one of three explicitly supported agents, with a dedicated blueprint
published as *NemoClaw for Hermes Agent*. It adds network egress policy with operator
approval flows, sandbox hardening (capability drops, process limits), managed/routed
inference, snapshots, and lifecycle operations via the NemoClaw CLI.

**C. OpenClaw under NemoClaw.** OpenClaw is NemoClaw's default agent and has the broader
messaging-channel surface. It was rejected because the product description is built around
Hermes' skill-extraction and memory model, and because NemoClaw treats the two as
interchangeable orchestration paths under the same governance layer — choosing Hermes costs
nothing in blueprint support.

## Decision

**Build on Hermes Agent, deployed under the NVIDIA NemoClaw blueprint.**

NemoClaw is adopted as the deployment and governance substrate for the project, not as an
optional hardening step to be evaluated later. Concretely:

- OpenShell sandboxing is the execution environment for the agent from the first working
  deployment onward.
- Network egress policy is the enforcement mechanism for data-handling rules. Prompt-level
  instructions are treated as UX, not as controls.
- Inference is routed through NemoClaw's managed inference configuration rather than the
  agent holding provider credentials directly.
- The NemoClaw CLI owns lifecycle operations (start, stop, snapshot, restore).

Implementation is phased (see `docs/PLAN.md`), and the phasing puts domain work before
production hardening. That is a sequencing decision about *when* each piece gets built —
it does not reopen the runtime choice. The target architecture is Hermes-on-NemoClaw
throughout, and the development environment tracks it from the start so the two do not
diverge.

## Consequences

### Accepted costs

- **Higher deployment floor.** ~~NemoClaw's documented targets are DGX-class systems or
  WSL. Bare Hermes runs on a $5 VPS. Cheap-VPS deployment is off the table.~~
  **Corrected 2026-08-27 — this was wrong; see the amendment below.** The real floor is
  4 vCPU / 8 GB RAM / 20 GB disk. Modest, but still above the cheapest VPS tier, and any
  hosting decision must satisfy NemoClaw's prerequisites.
- **Two unfamiliar systems at once.** When the agent misbehaves early on, the cause may lie
  in Hermes, in the sandbox policy, or in the interaction between them. Phase 0 exists
  specifically to build the operator familiarity that makes this diagnosable.
- **Blueprint drift.** NemoClaw is young and moving. Pinning a version and scheduling
  deliberate upgrades is now a maintenance obligation.
- **Domain mapping is ours.** NVIDIA's published Hermes+NemoClaw material is framed around
  research workflows. Nothing in the blueprint concerns ticket ingestion, customer
  profiles, escalation thresholds, or evaluation. That work is unchanged by this decision
  and remains the bulk of the project.

### Benefits

- Data-handling rules become enforceable rather than advisory: an agent that has been
  argued into exfiltrating customer data still cannot reach an unapproved endpoint.
- Self-written skills execute inside a hardened sandbox, bounding the blast radius of a bad
  skill.
- Snapshots give a rollback path for when the skill library degrades — the failure mode
  flagged in the README's own notes.
- Credentials live in the inference routing layer rather than in agent configuration.

### Rejected alternative worth recording

Deferring NemoClaw adoption until the shadow-mode-to-autosend transition was considered.
It would have kept early iteration cheap and let the egress policy be written against
observed traffic rather than predicted traffic. It was rejected because retrofitting a
sandbox boundary onto a system built without one tends to surface as a long tail of
"this worked locally" breakage, and because the prototype would accumulate habits — direct
credential use, unrestricted egress — that the target architecture forbids.

## Open questions

These were not resolved at decision time; NVIDIA's documentation domains
(`docs.nvidia.com`, `developer.nvidia.com`, `build.nvidia.com`) were not reachable when this
was written. Each must be confirmed against primary sources before Phase 0 exits.

1. ~~**Prerequisites.**~~ **Answered 2026-08-27 — see amendment.**
2. ~~**Inference providers.**~~ **Answered 2026-08-27 — see amendment.**
3. **Egress policy granularity.** Whether policy is expressible per-destination with
   operator approval at the granularity the escalation design needs.
4. **Gateway networking.** How the Hermes messaging gateway's inbound connections interact
   with sandbox network policy.
5. **The `nemoclaw-light` Hermes skin.** The README mentions a managed skin installed when
   connecting from light terminals; its relevance to a gateway-driven deployment is unclear.

## Amendment 2026-08-27

Two of the five open questions are closed. `docs.nvidia.com` is still unreachable from the
development environment, but **the documentation source is committed to the NemoClaw
repository** as `docs/**/*.mdx` and is readable over `raw.githubusercontent.com`. That is
the access route for the three questions that remain.

### Q1 — Prerequisites: answered

From `docs/get-started/prerequisites.mdx`:

| | |
|---|---|
| Minimum | 4 vCPU · 8 GB RAM · 20 GB disk |
| Recommended | 4+ vCPU · 16 GB RAM · 40 GB disk |
| Software | Docker (Engine / Desktop / Colima), Node.js 22.19+, npm 10+, Python 3 |
| Platforms | Linux (Ubuntu 24.04 primary), macOS on Apple Silicon, Windows via WSL2, DGX |
| GPU | **Not mentioned as a requirement** |

**A local GPU is not required.** The original ADR read the README's *express install*
framing — "on a supported DGX or WSL host" — as the general hardware requirement. It is not;
express install is one path among several.

**Consequence:** open decision 3 in `PLAN.md` (hosting) is unblocked. The target is a
commodity 8 GB VM, not DGX-class hardware. The "higher deployment floor" cost above was the
single largest accepted cost of this decision, and it was overstated. The decision itself
does not change — the argument for it was always the enforcement boundary, not the hardware
— but the cost/benefit that justified adopting NemoClaw up front rather than deferring it
is now materially better than recorded.

### Q2 — Inference providers: answered

`docs/inference/` ships dedicated setup pages for **Anthropic, OpenAI, Google Gemini,
OpenRouter, and NVIDIA endpoints**, alongside local serving via Ollama, llama.cpp, vLLM, and
NIM, plus a Hermes provider and a model router.

**Consequence:** hosted inference on commodity hardware is a first-class supported path, and
open decision 2 in `PLAN.md` (model) is constrained only by preference. Local serving via
Ollama is also the free path used by the inbox agent today
(`docs/INBOX_AGENT.md`, D-010).

### Still open

Q3 (egress policy granularity), Q4 (gateway networking), and Q5 (`nemoclaw-light`) remain
unanswered. All three are answerable from the same source. They do not block the inbox
agent, which runs outside the sandbox by design (D-005); they do block Phase 0 exit.

## Notes

- `TheAiSingularity/hermesclaw` appears in searches as a Hermes+OpenShell integration. It
  is a third-party repository, not NVIDIA-published. `NVIDIA/NemoClaw` is the source of
  truth for this project.
- Licensing: Hermes Agent is MIT, NemoClaw is Apache 2.0. Both are compatible with the
  project's intended use; no copyleft obligations attach.
