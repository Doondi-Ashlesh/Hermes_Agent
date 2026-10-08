# Decision & failure log

Why things are the way they are, what broke, and whether it got fixed.

Entries are short by design: reasoning and outcome, not a narrative. Detail
belongs in the code, the tests, or the doc the entry points at.

- **`D-nnn`** decision — needs a rejected alternative, or it isn't a decision
- **`F-nnn`** failure — what was wrong, how it was caught, fixed or not
- **`O-nnn`** open — known and unresolved

---

## Failures

### F-018 · `doctor` crashed on an Ollama host with a trailing slash
**Why:** it split `HERMES_OLLAMA_HOST` on ":" by hand, so `http://localhost:11434/` made
`int("11434/")` raise — a traceback from the command that exists to diagnose a bad setup.
Found next to a type-checker warning about the same lines, not by the type checker itself.
**✅ Fixed** — both provider probes parse the URL with `urlparse`; a test covers the slash,
a missing port, and https.

### F-017 · Relabeling a message kept both answers
**Why:** the store is append-only and every reader took every line, so correcting the same
message twice put it in the prompt with both labels, counted it twice, and scored it twice
in `eval` against opposite answers. Found reviewing PR #12.
**✅ Fixed** — `FeedbackStore.current()` keeps the latest label per message, in order of
when it was decided; the prompt, counts and eval read that. `all()` keeps the full
history, and nothing in the file is rewritten. Tests reproduce the bug first.

### F-016 · PR #12 shipped from a branch that broke the naming rule
**Why:** the work started on a session-assigned branch (`claude/…`), which is neither
`<layer>/<what-is-new>` nor free of tool names (CONTRIBUTING §4, §5). Not caught before the
PR was opened; the owner caught it after merge.
**Partly fixed** — the merged commit is also on `learning/eval-intervals`; the old branch
needs deleting by hand (the push to delete was refused here). `main`'s merge message keeps
the old name: rewriting `main` to fix a label is not worth it. Branch is now chosen first.

### F-015 · The eval reported point estimates that four examples cannot support
**Why:** "recall 100%" on 4 important emails read as a result; its 95% interval is [51%, 100%].
The replay also rebuilt messages from stored corrections without headers — so it scored a
different input than the live loop saw — and re-read `feedback.jsonl` once per example.
**✅ Fixed** — every rate carries a Wilson interval; the golden set replays full messages,
headers included; the store is read once (tested). Corrections now store the source's
headers, and replay uses them.
**Correction:** this entry first said past corrections were "not fixable" because their
headers were never stored. Wrong — the decision log keeps every classified message whole,
so `eval` recovers them from there. Only a correction whose decision is gone replays bare.

### F-014 · `doctor` reported keychain secrets as coming from the environment
**Why:** `load_into_env` copies keychain values into `os.environ`, and `inspect` ran
afterwards and saw its own injection — reporting "environment" for exactly the secrets
whose location the command exists to explain.
**✅ Fixed** — the loader records what it injected and `inspect` discounts it. Caught by a
test-isolation failure: the variable that leaked between tests leaked by the same
write-through the real bug used.

### F-013 · A transient blip silently dropped a notification
**Why:** the Anthropic SDK retries; the urllib paths (Ollama, Telegram) did not, so one
refused connection lost that alert or classification outright.
**✅ Fixed** — `http.py` retries connection errors, 408, 429 and 5xx with exponential
backoff plus jitter, honouring `Retry-After`. Other 4xx are **not** retried: a bad token
fails identically every time. Tests assert both halves of that policy.

### F-012 · The test suite ran only when someone remembered
**Why:** no CI ever existed. Tests only ran when someone remembered.
**✅ Fixed** — GitHub Actions on every push and PR: suite across Python 3.10-3.13 with
the API key explicitly blanked, doc consistency, mermaid parsing, and an end-to-end demo
that asserts the injection fixture is still suppressed.

### F-011 · A crash mid-write could truncate `state.json`
**Why:** `save()` wrote in place. A partial file reads back as "no cursor", which
re-notifies the entire mailbox.
**✅ Fixed** — write to a temp file, fsync, then `os.replace`. Test simulates a failure
mid-save and asserts the previous cursor survives with no debris left.

### F-010 · `DecisionLog.find()` parsed the whole file, once per correction
**Why:** `find()` called `all()`. Measured at a year of mail (36.5k decisions, 33.6 MB):
**~1.0s per lookup**, and one lookup happens per button press.
**✅ Fixed** — in-memory uid→offset index, built by scanning offsets without parsing JSON,
updated on append, rebuilt on a miss or stale hit. **1010ms → 10ms (101x).** Regression
test fails if lookups start scaling with log size again.

### F-009 · A crash mid-cycle re-sent every notification since the last save
**Why:** `set_last_uid` updated memory per message but `save()` only ran after the whole
loop. SIGTERM or a crash lost the advance and replayed it.
**✅ Fixed** — save per message, and save the notifier offset before processing (consumed
`getUpdates` cannot be re-fetched). Test kills a cycle mid-way and asserts the resumed run
starts at the right message.

### F-008 · Docs claimed test counts that were three different wrong numbers
**Why:** `87`, `100` and `116` were hand-written at different times and drifted.
**✅ Fixed** — a test reads the real collected count and fails on any doc quoting a
different one. Small lies make a reader distrust the whole page.

### F-007 · Doc cross-references broke while writing this log
**Why:** added anchor links faster than they could be verified by hand.
**✅ Fixed** — `scripts/check_links.py` plus `tests/test_docs.py` now fail the build on a
broken link, stale env var, or undocumented command.

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
needed quoting. Now a standing rule in `CONTRIBUTING.md`.

---

## Decisions

### D-023 · Bounded, tested, audited dependencies
**Why:** `anthropic>=1.0` accepted any future release, so a breaking 2.0 would have broken a
fresh install with no change here; and nothing checked dependencies for known
vulnerabilities.
**Bounds:** `anthropic>=1.0,<2`, `keyring>=25,<26`. The floors were verified, not assumed:
the suite passes on `anthropic==1.0.0` with `keyring==25.0.0`, and a CI job keeps running
it there. Ceilings sit at the next major; Dependabot proposes the bump and CI decides.
**Audit:** `pip-audit`, pinned, on every push and weekly, since a vulnerability is
published against a dependency rather than a commit. Clean at the time of writing.
**Rejected:** a lockfile. This is installed as a package, where exact pins would
conflict with whatever else shares the environment; bounds plus a tested floor is the
library-shaped answer. Revisit if it ever ships as a container image.

### D-022 · Lint and type-check in CI, with the tools pinned
**Why:** the suite tested behaviour but nothing checked the code itself — the first gap a
reviewer from a larger engineering org would see. `ruff` lints with an explicit rule set
(the tool's defaults move between releases) and `mypy` type-checks the package. Both are
pinned exactly, so a new release cannot turn CI red on an unchanged commit.
**What it found:** 36 lint findings and 8 type errors, all fixed. Most were style; the
useful ones were `zip()` silently truncating mismatched label/score lists in `metrics.py`
(now `strict=True`) and F-018.
**The formatter came separately** (a first draft of this entry said 33 files; at the
configured width of 100 it was 22). One commit, layout only, verified by comparing every
file's AST before and after, then listed in `.git-blame-ignore-revs` so `git blame` skips
it. Line length (E501) was switched on with it; CI now checks formatting too.

### D-021 · The `openai-compat` key is a secret like the others
**Why:** it sat in plaintext `.env` with no way out, while the other three could move to the
keychain (D-018). Added to `secrets.SECRETS`, it gets the same resolution order, `import`,
`set`, `status`, and shadow warning for free — and nothing else had to change, which is the
seam working.
**Optional, so silent when absent:** `doctor` lists only the secrets it finds, so a local
vLLM server with no key reports nothing missing. Tested.
**Rejected:** a separate store for optional secrets. One list, one precedence rule.

### D-020 · A fourth provider for any OpenAI-compatible server
**Why:** vLLM and NVIDIA NIM both serve `/v1/chat/completions`, and they are where a
self-hosted model actually runs at throughput — Ollama is the laptop path, not the GPU one.
One adapter covers both, keeps mail on hardware you control, and gives `eval --compare` a
second local contender.
**Shape:** constrained to the schema via `response_format` json_schema; output still goes
through `coerce_verdict` (shared with Ollama now) because constrained decoding fixes the
shape, not the values. Out-of-range values are clamped; a missing, non-numeric or NaN score
**raises**. Ollama used to raise on a non-numeric score only by accident, and defaulted a
missing one to 0 — which would mark a message unimportant and advance the cursor past it. The served model is discovered from `/v1/models` under a lock, and
`eval` resolves it before fanning out so a dead server fails once, not once per worker.
**Rejected:** the `openai` SDK — a dependency for one POST, and `http.py` already carries
the retry policy (F-013). Naming it `nim` — the transport is generic, and NIM is unverified
(O-004).

### D-019 · The eval reports estimates with intervals, and a golden set gates CI
**Why:** the harness was the thing that makes the correction loop falsifiable, and it was
reporting bare percentages from a handful of labels. Now: Wilson intervals on every rate,
Brier and ECE because the gate thresholds the score, the strictest threshold reaching a
recall target, p50/p95 latency, and an exact McNemar test for `--compare`, since two
providers scored on the same examples are paired samples.
**Why Wilson, not bootstrap:** deterministic, closed-form, and sane at 0/n and n/n — which
is exactly where a small personal label set lives. **Why exact McNemar:** the chi-squared
form needs ~25 discordant pairs.
**Golden set:** `fixtures/labels.json`, all 12 fixtures labeled with a reason. CI runs it
with `--min-recall 1.0`, which closes PLAN Phase 4's "runs in CI, baseline recorded" for
Track A. The offline rules were written against these fixtures, so the gate is a regression
tripwire, not evidence of quality.
**Rejected:** a threshold auto-tuner that rewrites `HERMES_THRESHOLD`. It would fit 4
positives exactly; the report suggests and warns below 10 instead.

### D-012 · The product is the machinery, not either deployment
**Why:** the repo said "customer support agent" and shipped an inbox agent. Measured the
split — ~766 loc source-agnostic vs ~382 mail-flavoured, of which only `imap.py` (97 loc)
is truly source-bound. The two differ in the *verb* (judge vs draft) and the *action*
(notify vs send), not in the pipeline.
**Rejected:** keeping support as the headline (README would describe something that
doesn't exist); dropping to inbox-only (discards ADR/PLAN framing that still applies to both).
**✅ Done** — README, PLAN and ARCHITECTURE reframed as one machine with two deployments.
Track A running, Track B blocked on tickets and a write scope.

### D-018 · Secrets can move to the OS keychain, which is consulted last
**Why:** the three secrets sat in plaintext `.env`, which backs up, syncs, and is readable
by anything running as you. `keyring` puts them in Keychain / Credential Manager / Secret
Service instead.
**Why last in precedence (environment → `.env` → keychain):** an existing install behaves
identically, the keychain is touched only for values nothing else supplied so an unused
one never prompts for an unlock, and a half-finished migration keeps working from `.env`
instead of silently reading a stale stored copy. The cost — a line left in `.env` shadows
the keychain — is surfaced by a `doctor` warning rather than hidden.
**Rejected:** having `import` strip the lines from `.env`. A tool that rewrites the file
holding your credentials can only ever lose them; it prints what to delete.
**Optional dependency on purpose:** there is no keychain on a headless server, which is
where this most often runs. Missing, locked or broken all degrade to `.env`; none of them
can stop the agent starting. `set` prompts rather than taking the value from argv, which
would land in shell history and `ps`. All four properties are tested.

### D-017 · Backfill classifies in parallel; the live loop never will
**Why:** backfill is latency-bound on hundreds of independent calls — 300 messages at ~2s
is ten minutes serially, ~2.5 at concurrency 4. Measured 0.61s → 0.15s on a 12-message
simulation.
**Why not the live loop:** it advances a strictly ordered cursor after every message, which
is precisely what F-004 and F-009 were about. Backfill touches no cursor, which is what makes
parallelism safe there and not here.
**Order is preserved anyway** — results are consumed in message order, so the decision log
stays ordered however the calls complete. First failure cancels the rest.

### D-016 · Drafting forces the source off IMAP
**Why:** egress policy granularity is protocol-dependent — method and path on HTTP, but only
host/port/binary on raw TLS, because OpenShell cannot see inside the TLS session. The stock
`gmail` preset opens IMAP 993 and SMTP 465 together with no read/send split.
**Consequence:** F-002 said "cannot send" must become a policy guarantee once drafting
exists; this says that guarantee is only achievable over the Gmail HTTP API. Track B needs
`sources/gmail.py`. Does not affect the read-only inbox agent, and does not invalidate
D-002 for what it does today.

### D-015 · A doctor command, and a LICENSE file
**Why (doctor):** every setup failure so far — wrong Gmail password, Ollama not running,
silent fallback to keyword rules — was diagnosable only by running the thing and reading a
traceback. `doctor` answers "what is missing and what do I type next" before any credential
exists, which is when it matters most.
**Two invariants, both tested:** it never prints a secret (values are masked or reported as
present/absent) and it never writes anything.
**Why (LICENSE):** `pyproject.toml` declared MIT with no LICENSE file, so the repo was
effectively unlicensed. Fixed.

### D-014 · Backfill and a sorted list, before any web UI
**Why:** the eval harness needs ~30 corrections and the live loop yields one decision per
new message, so `O-003` was weeks out. `backfill` classifies existing mail; `list` sorts it
by score with the summary and suggested action that were already being computed and thrown
away. Both are terminal-only.
**Rejected for now:** a web review UI. It would be a view over data that did not exist yet,
and the decision log holds real senders and subjects — so it would have to be local-only
anyway. Revisit once labelling in bulk actually hurts.
**Backfill is deliberately inert:** notifies nobody, never moves the read cursor, skips what
it has already classified. Tests assert all three.

### D-013 · Logging for library code, printing for the CLI
**Why:** 24 `print()` calls, no levels, no timestamps, nothing parseable — unusable for a
daemon under systemd. `logs.py` gives text or JSON output with the level from env.
**The split:** library code logs (telemetry, `DEBUG` shows every decision with score and
gate rule); the CLI prints (output the user asked for, which must not vanish at a higher
log level). Notifications stay on stdout — they're the product, not diagnostics.
**Rejected:** a logging dependency. stdlib `logging` with two formatters is enough.

### D-011 · Docs written for replication, and tested
**Why:** the docs explained *what* and *why* but a new engineer couldn't rebuild or
extend without reverse-engineering. Added `SETUP.md` (verify step per stage),
`EXTENDING.md` (one email traced through the code + four recipes) and a `Makefile`.
**Tested, not trusted:** `make check` now fails on a `make` target that doesn't exist,
a symbol a recipe tells you to import that doesn't, a module missing from the file
inventory, or a stale test count. Verified by wiping `.venv` and replaying the doc.

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

### O-004 · `openai-compat` is verified against vLLM, not NIM
The request shape follows vLLM's structured-outputs docs; NVIDIA's docs were unreachable
from the build environment, so NIM support for `response_format` json_schema is expected,
not confirmed. Closing it takes one `eval --golden` against a running NIM container.
The other half of this entry — `HERMES_OPENAI_API_KEY` not being keychain-backed — is
closed: it is a fourth, optional secret (D-021).

### O-003 · Not validated against real mail
Every accuracy claim is against 12 fixtures. Gates the next phase: strong recall → reply
drafting; weak recall → per-sender memory first. The instrument is now ready (D-019): what
is missing is ~30 labels from a real mailbox. "Strong" should be read off the interval's
lower bound, not the point estimate.

### O-002 · Phase 0 install not done
All five ADR 0001 questions are now answered (2026-09-09 amendment), so what blocks Phase 0
exit is the stock install, runbook and version pins — a host and an afternoon, not research.
One detail stayed undocumented: whether a messaging channel polls or receives webhooks.
Either is covered by that channel's egress preset, so confirm it during the install.

### O-001 · Corrections grow unboundedly
`HERMES_MAX_EXAMPLES` caps what reaches the prompt; no pruning or conflict detection.
Contradictory corrections across *different* messages would still fight silently — only
the eval score would show it. Two labels on the *same* message no longer can (F-017).
