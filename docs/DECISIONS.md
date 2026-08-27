# Decision & failure log

Why things are the way they are, what broke, and whether it got fixed.

Entries are short by design: reasoning and outcome, not a narrative. Detail
belongs in the code, the tests, or the doc the entry points at.

- **`D-nnn`** decision — needs a rejected alternative, or it isn't a decision
- **`F-nnn`** failure — what was wrong, how it was caught, fixed or not
- **`O-nnn`** open — known and unresolved

---

## Failures

### F-006 · ADR 0001 claimed DGX-class hardware was required
**Why:** read the README's *express install* line as the general requirement; the
primary source was unreachable and the gap was never revisited.
**Actually:** 4 vCPU / 8 GB / 20 GB, no local GPU. Found by re-checking whether the
open questions were still blocked — NemoClaw's docs are committed as `docs/**/*.mdx`
and readable via raw.githubusercontent even though `docs.nvidia.com` is proxy-blocked.
**✅ Fixed** — ADR amended, Q1/Q2 closed, PLAN decisions 2 and 3 resolved.

### F-005 · Cost estimates given to the user were wrong twice
**Why:** quoted cache savings from memory. Haiku 4.5 needs a 4,096-token prefix to
cache; ours is ~2,200, so caching does nothing there — silently, no error. And
caching scales with request *density*: ~33% at 4 emails/hour, not the ~90% headline.
**✅ Fixed** — default moved to Sonnet 5, real numbers and both caveats in `INBOX_AGENT.md`.

### F-004 · A provider outage would have discarded the mailbox
**Why:** on classify failure the loop logged the error and still advanced the read
cursor, marking unclassified mail seen forever. Caught by smoke-testing `--provider
ollama` with no server running — 12 identical errors looked like log noise, weren't.
**✅ Fixed** — cycle now stops without advancing; retried next poll. Two tests pin it.
**Trade:** a permanently bad message blocks the queue, loudly. Preferred to silent loss.

### F-003 · `interval or self.config.interval` treated 0 as unset
**Why:** `0` is falsy, so an explicit zero fell through to the 60s default.
Caught because the suite took 60s and `--durations` put all of it in one test.
**✅ Fixed** — explicit `None` check. Suite 60.11s → 0.06s.

### F-002 · Expected a Gmail read-and-draft-but-cannot-send scope
**Why:** assumed shadow mode could be enforced by OAuth scope. No such scope exists —
`gmail.compose` covers drafting *and* sending.
**Not a bug, a constraint.** No impact today (read-only, no send path). Matters when
drafting is added: "cannot send" becomes policy, not credential — which is ADR 0001's
argument for enforcement below the agent.

### F-001 · Mermaid diagrams were nearly pushed unvalidated
**Why:** no check existed; a syntax error would have rendered as raw text on GitHub.
**✅ Fixed** — all blocks parsed with mermaid's own parser before push; two edge labels
needed quoting. Now a standing rule in `CLAUDE.md`.

### F-000 · Doc cross-references broke while writing this log
**Why:** added anchor links faster than they could be verified by hand.
**✅ Fixed** — `scripts/check_links.py` plus `tests/test_docs.py` now fail the build on a
broken link, stale env var, or undocumented command.

---

## Decisions

### D-010 · Provider registry with three implementations
`auto | anthropic | ollama | offline`. Three because a seam with one implementation is
an assertion, not an abstraction — Ollama proves a different transport, `offline` proves
a non-model path and lets CI run with no credentials.
**Rejected:** hosted free tiers — volume would fit, but the data trains their models.

### D-009 · Corrections live in the cached system prefix
They're ~1,750 of ~2,200 input tokens and change only when the user corrects something.
In the user turn they were re-billed at full price every call. Worth less than expected —
see F-005.

### D-008 · Learning is in-context few-shot, not fine-tuning
Fine-tuning needs volume the user lacks, has no rollback, and makes regressions
undiagnosable. **Consequence:** the learning mechanism *is* instruction-following under
conflicting examples — the most model-sensitive part of the system. Drives D-010 and F-005.

### D-007 · Eval is leave-one-out
Scoring an example while its own answer sits in the prompt measures nothing. Watch recall:
a false positive is one buzz, a false negative is an email never seen.

### D-006 · Read-only by construction, not by policy
`readonly=True` + `BODY.PEEK[]`, and no send path anywhere. Makes "cannot damage your
mailbox" a property of the code rather than a promise. Also why D-005 holds.

### D-005 · Ship outside the NemoClaw sandbox for now
Blast radius is an unwanted notification; the sandbox isn't earning its cost yet.
Changes the moment a write scope appears. Sequencing, not a reversal of ADR 0001.

### D-004 · Polling, not IMAP IDLE
60s is indistinguishable from push at human timescales, needs no reconnect state machine,
and is the same code path against fixtures and a live server. **Cost:** up to 60s latency.

### D-003 · Telegram before WhatsApp
BotFather takes two minutes; WhatsApp Business API needs a business account, a number, and
template approval. `Notifier` is the seam, so WhatsApp drops in later unchanged.

### D-002 · IMAP before the Gmail API
Any provider, app password only, no OAuth consent screen. Labels and threading don't pay
for that friction on day one.

### D-001 · Inbox agent as the first deployable use case
The support-bot premise needs ticket data that doesn't exist; a mailbox supplies both live
traffic and the historical corpus PLAN lists as unknown. Exercises ADR 0001's
swap-the-source claim rather than working around it.

---

## Open

### O-003 · Not validated against real mail
Every accuracy claim is against 12 fixtures. Gates the next phase: strong recall → reply
drafting; weak recall → per-sender memory first.

### O-002 · Three ADR 0001 questions still open
Egress granularity, gateway networking, `nemoclaw-light`. Answerable from NemoClaw's
committed docs (route in F-006). Blocks Phase 0 exit, not current work.

### O-001 · Corrections grow unboundedly
`HERMES_MAX_EXAMPLES` caps what reaches the prompt; no pruning or conflict detection.
Contradictory corrections would fight silently — only the eval score would show it.
