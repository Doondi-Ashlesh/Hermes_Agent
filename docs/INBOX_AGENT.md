# Inbox agent — deployment 1

The first of the two deployments in [`README.md`](../README.md), and the one that
runs today. It needs a mailbox you already own rather than the production ticket
data the support agent is waiting on.

It is the shared machinery with a mail source and a notify action.
[ADR 0001](adr/0001-runtime-nemoclaw-hermes.md)'s first boundary promises that
"swapping Zendesk for email must not touch agent code" — this is that swap, and
every box in [`ARCHITECTURE.md`](ARCHITECTURE.md) keeps its shape.

## What it does

Polls a mailbox. For each new message: redact → classify → apply deterministic
policy → ping you on Telegram if it matters. You answer the ping with a button,
and that correction goes into the next classification.

```
IMAP (read-only)  →  redact  →  classify (Claude)  →  policy gate  →  Telegram
                                     ↑                                    │
                                     └────── corrections ◀── your button ─┘
```

## Run it now

```bash
make install && make demo
```

12 fixture messages go through the whole pipeline with a keyword classifier;
four of them would have interrupted you. No credentials needed.

For a real mailbox — model access, IMAP, Telegram, and the gate rules, each with
a verification step — follow **[SETUP.md](SETUP.md)**. To change any of it, see
**[EXTENDING.md](EXTENDING.md)**.

## Commands

| Command | What it does |
|---|---|
| `hermes-inbox demo` | Fixture mailbox, console output, no credentials |
| `hermes-inbox once` | One polling cycle against your real mailbox, then exit |
| `hermes-inbox run` | Poll continuously |
| `hermes-inbox feedback <uid> important\|not-important --note "..."` | Correct a call from the terminal |
| `hermes-inbox eval` | Replay every correction and score the classifier, with 95% intervals |
| `hermes-inbox eval --golden` | Same, against the 12 labeled fixtures — no corrections or credentials needed |
| `hermes-inbox eval --compare anthropic,ollama` | Score providers on the same cases, with a paired significance test |
| `hermes-inbox eval -v` | Adds the reliability table and the full threshold sweep. `--json` for machines |
| `hermes-inbox backfill --days 30` | Classify mail already received. Does **not** notify |
| `hermes-inbox backfill --concurrency 8` | Same, wider. Default 4; `1` is serial |
| `hermes-inbox list` | Sorted list of decisions with summary and suggested action |
| `hermes-inbox doctor` | Check the setup and print the single next thing to do |
| `hermes-inbox secrets` | Where each secret is resolving from. Also `set`, `rm`, `import` |
| `hermes-inbox stats` | What it has processed, by category and by gate rule |

All of `demo`, `once` and `run` accept `--log-level DEBUG` and `--log-format json`.
`DEBUG` prints every decision with its score and the gate rule that fired; `json`
emits one object per line for `journalctl -o cat | jq`.

## How the learning actually works

Be precise about this, because "self-improving agent" is usually doing a lot of
unearned work in a sentence.

There is no fine-tuning and no weight update. What happens is:

1. Every decision is written to `data/decisions.jsonl`.
2. When you press **🔕 Not important** (or run `hermes-inbox feedback`), a
   labeled example is appended to `data/feedback.jsonl`.
3. On every subsequent classification, your most recent corrections are rendered
   into the prompt above the message being judged, with a note that they
   outrank the general guidance.

Correcting the same message again replaces your earlier answer: the prompt, the
counts and `eval` use only the latest label per message, and a relabel counts as
your newest correction. The file keeps every press, so the change of mind stays
on record.

So it stops making a mistake because you told it not to, and the telling
persists. That is a real feedback loop, and it is also the whole of it.

### Getting to 30 corrections without waiting a week

The live loop produces one decision per new message, so a useful eval score is
weeks away. `backfill` classifies mail you already have:

```bash
hermes-inbox backfill --days 30      # asks before spending model calls
hermes-inbox list --unlabeled --min-score 0.5
hermes-inbox feedback <uid> important --note "why"
```

Backfill deliberately does three things differently from the live loop: it
notifies nobody, it leaves the read cursor alone so it cannot make `run` skip
new mail, and it skips messages already in the decision log so it is safe to
re-run.

It also classifies in parallel — 300 messages at ~2s a call is ten minutes
serially and about two and a half at the default concurrency of 4. Decisions are
still written in message order, so the log stays ordered however the calls
finish. **The live loop is always serial** and stays that way: it advances an
ordered cursor after every message, and reordering that is exactly how
[F-004](DECISIONS.md#f-004--a-provider-outage-would-have-discarded-the-mailbox)
and [F-009](DECISIONS.md#f-009--a-crash-mid-cycle-re-sent-every-notification-since-the-last-save)
happened. Raise `--concurrency` if your provider tolerates it; lower it to `1`
if you hit rate limits.

`list` sorts by score by default (`--sort date|sender`) and filters with
`--category`, `--min-score`, `--needs-action` and `--unlabeled`. Each row shows
the one-line summary and the suggested action that were already computed at
classification time.

### Why the eval harness is the important half

Corrections can conflict, drown each other out, or overfit to one strange week
of mail. Without a way to measure, "it's getting better" is unfalsifiable — the
exact failure mode [PLAN.md](PLAN.md) flags for the skill library.

`hermes-inbox eval` replays every stored correction back through the classifier
with **that example excluded from the prompt** (leave-one-out — scoring an
example while the answer sits in its own context measures nothing) and reports.
This is the real output of `hermes-inbox eval --golden --provider offline`:

```
Replayed 12 labeled example(s) (4 important), leave-one-out, threshold 0.7.

               value   95% interval     n
  accuracy   100.0%  [75.8%, 100.0%]  12/12
  precision  100.0%  [51.0%, 100.0%]  4/4   of the pings, how many you wanted
  recall     100.0%  [51.0%, 100.0%]  4/4   of what mattered, how much it caught
  f1         100.0%

  hits 4  ·  correct silences 8  ·  false alarms 0  ·  missed 0

  calibration  brier 0.039 · ece 0.156   (0 is perfect; brier 0.25 is a coin flip)
  latency      p50 0ms · p95 0ms · 12 calls in 0.01s
  threshold    0.8 is the strictest reaching recall ≥ 95% (precision 100%, 4 ping(s))
               only 4 important example(s) — fitted to noise below 10; do not act on it yet
```

Watch **recall**. A false positive is one unnecessary buzz; a false negative is
an important email you never saw. They are not equally bad.

**Read the interval, not the value.** A perfect score on 4 important emails is
consistent with a classifier that catches only half of them — that is what the
51% lower bound says. Intervals are Wilson score intervals, chosen because the
textbook normal interval claims [100%, 100%] from four samples. The rest of the
report:

- **Calibration.** The gate thresholds the score, so the score has to mean what
  it says. Brier is the mean squared error of the score as a probability; ECE
  is how far "said 0.9" sits from "was important 90% of the time". `-v` prints
  the reliability table behind it.
- **Threshold.** The strictest threshold that still reaches `--target-recall`
  (default 95%), derived from your labels instead of assumed. Below 10 important
  examples it is flagged as noise; `-v` prints the whole sweep.
- **Latency.** p50 and p95 per call. `--concurrency` parallelizes the replay the
  way `backfill` does — no cursor moves, so it is safe here.

### The golden set, and the CI gate

`fixtures/labels.json` labels all 12 fixtures, each with a one-line reason; uid
109, the injection attempt, is labeled not important. `eval --golden` replays it
with no corrections and no credentials, so it runs in CI on every push:

```bash
hermes-inbox eval --golden --provider offline --min-recall 1.0
```

`--min-recall` fails the build if the point estimate drops. The recorded
baseline for the offline rules is the output above: 4/4 caught, 0 false alarms.

It is a regression check, not a measurement. The offline rules were written with
these same twelve messages in view, so 4/4 is expected and says nothing about
quality — the gate exists to catch a change that breaks what used to work.
Twelve messages cannot say how the agent does on your mail — only your own labels can
([O-003](DECISIONS.md#o-003--not-validated-against-real-mail)).

## The policy gate

The model produces a score. The gate decides whether you get interrupted, in
deterministic code, in this order:

1. `HERMES_NEVER_SENDERS` — an explicit mute, whatever the model thinks
2. `HERMES_ALWAYS_SENDERS` — an explicit escalation, beats a low score
3. `HERMES_ALWAYS_KEYWORDS` — subject-line triggers you set by hand
4. `HERMES_MUTED_CATEGORIES` — whole classes you never want pinged about
5. `HERMES_QUIET_HOURS` — time-of-day suppression
6. `HERMES_THRESHOLD` — only now is the model's score consulted

Every decision records which rule fired (`hermes-inbox stats` breaks it down),
so when it does something surprising you can see why rather than guess.

Senders match as a full address (`a@b.com`) or a bare domain (`b.com`).

## Safety properties

- **Read-only mail access.** The mailbox is selected `readonly=True` and bodies
  are fetched with `BODY.PEEK[]`, which does not set `\Seen`. Running this
  cannot alter your mailbox. Use an app password, never your main password.
- **It cannot send email.** There is no send path in the code at all.
- **Redaction before the model.** Card numbers, phone numbers, one-time codes,
  API keys, and URL credentials are replaced with placeholders before any text
  is sent to the provider. Sender and subject are preserved deliberately —
  importance is mostly a function of who wrote to you.
- **Secrets can live outside the filesystem.** Three values are secret — the
  model key, the mailbox password, the bot token. `hermes-inbox secrets import`
  moves them from `.env` into the OS keychain, and `hermes-inbox secrets`
  reports where each one is resolving from without printing any of them. See
  [Where secrets live](#where-secrets-live).
- **Ticket text is data, not instructions.** The classifier prompt says so, and
  `fixtures/inbox.json` includes an adversarial message (uid 109) that tries to
  talk the agent into flagging itself as important. It scores 0.02 as spam, and
  a test asserts that.

### Where secrets live

Three values are secret: `ANTHROPIC_API_KEY`, `IMAP_PASSWORD` and
`TELEGRAM_BOT_TOKEN`. Everything else in the config is a hostname, a threshold,
or a preference.

By default they sit in plaintext in `.env`, which is defensible on a laptop you
alone use and poor anywhere else — `.env` survives into backups, syncs to cloud
folders, and is readable by anything running as you. Install the extra and move
them into the OS keychain instead:

```bash
pip install 'hermes-inbox[keyring]'
hermes-inbox secrets import      # reads .env, stores each in the keychain
hermes-inbox secrets             # shows where each one now resolves from
```

Resolution order, highest first:

| | Source | Used for |
|---|---|---|
| 1 | environment variable | containers, systemd `Environment=`, one-off overrides |
| 2 | `.env` | the default; a laptop you control |
| 3 | OS keychain | Keychain, Credential Manager, or Secret Service |

**The keychain is last on purpose.** An existing `.env` install keeps behaving
exactly as it did; the keychain is consulted only for values nothing else
supplied, so a machine that does not use it pays no DBus round-trip and is never
prompted to unlock anything; and a half-finished migration keeps working from
`.env` rather than silently reading a stale stored copy.

The cost of that order is that a value left in `.env` shadows the keychain one.
`import` does not edit `.env` — a tool that rewrites the file holding your
credentials can only ever lose them — so it prints the lines to delete, and
`doctor` keeps warning until they are gone:

```
  ! IMAP_PASSWORD    in the keychain, but .env wins
                       → remove IMAP_PASSWORD from .env to use the stored one
```

There is no keychain on a headless server, which is where this most often runs.
That is why `keyring` is an optional extra and why every path degrades to `.env`
rather than failing: a locked, broken, or absent keychain must not stop the
agent from starting. `HERMES_KEYRING=off` skips it entirely. For a systemd unit,
prefer an environment variable from a root-owned file over either:

```ini
[Service]
EnvironmentFile=/etc/hermes-inbox.env    # chmod 600, root-owned
```

`hermes-inbox secrets set <NAME>` prompts for the value rather than taking it as
an argument — a command-line argument would land in shell history and in anyone
else's `ps` output.

## Choosing a provider

The classifier is a seam. Four implementations ship:

| `HERMES_PROVIDER` | Cost | Learns from corrections | Notes |
|---|---|---|---|
| `anthropic` *(default)* | ~$9–30/mo | yes | Best judgement and injection resistance |
| `ollama` | free, local | yes, less reliably | Needs `ollama serve`; nothing leaves your machine |
| `openai-compat` | your GPU | yes, model-dependent | Any `/v1/chat/completions` server — vLLM, an NVIDIA NIM container. Nothing leaves your hardware |
| `offline` | free | **no** | Keyword rules. Demos and CI only |
| `auto` | — | — | `anthropic` if a key is set, else `offline` |

```bash
HERMES_PROVIDER=ollama HERMES_OLLAMA_MODEL=qwen2.5:7b hermes-inbox once
```

### Self-hosted on a GPU: `openai-compat`

For a model served by vLLM or an NVIDIA NIM container — the serving stacks built
for throughput, which is what `eval --concurrency` and `backfill --concurrency`
are bound on:

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct          # listens on :8000
HERMES_PROVIDER=openai-compat hermes-inbox eval --golden --concurrency 8
```

- Output is constrained with `response_format: {"type": "json_schema"}`, the
  request shape vLLM documents for structured outputs. A server that rejects it
  fails with an error naming structured outputs, not a parse error.
- `HERMES_OPENAI_MODEL` can stay empty: a vLLM or NIM server serves one model, and
  it is read from `/v1/models` once per process.
- `HERMES_OPENAI_API_KEY` is optional and sent only when set. It is a secret
  like the other three: `hermes-inbox secrets set HERMES_OPENAI_API_KEY` keeps
  it in the OS keychain instead of `.env`.
- NIM is expected to work because it serves the same OpenAI-compatible API, but
  that has not been verified against a running NIM container (O-004).

Before switching, measure it against what you run now, on the same cases:

```bash
hermes-inbox eval --compare anthropic,openai-compat
```

### What it costs

Measured against this prompt: **~2,200 input tokens, ~150 output** per email.
At 100 emails/day:

| Model | Uncached | Cached | Min. prefix to cache |
|---|---|---|---|
| Opus 5 | $44/mo | $30/mo | 512 tok |
| **Sonnet 5** *(default)* | $18/mo | **$12/mo** | 1,024 tok |
| Haiku 4.5 | $9/mo | *cannot cache* | 4,096 tok |

Two things are worth knowing before optimizing this:

**Haiku 4.5 cannot cache this prompt.** Its minimum cacheable prefix is 4,096
tokens and ours is ~2,200. Below the minimum, caching silently does nothing —
no error, just `cache_creation_input_tokens: 0`. That closes the gap between
Haiku and Sonnet 5 to about $3/month, which is why the default is Sonnet 5: the
correction loop is in-context learning, and that is precisely the capability
that rewards the better model.

**Caching pays off with request density, not volume.** At ~4 emails/hour you pay
a 2× cold write to amortize over ~3 reads, so a 1-hour TTL saves about a third
rather than the ~90% headline. It earns much more during `hermes-inbox eval`,
which replays every correction back-to-back and hits a warm cache every time.

Set `HERMES_MODEL` to override, and `HERMES_EFFORT=low` to cut spend further.

### Deciding empirically

Do not take the table above on faith for *your* mail. Label ~30 messages, then
score each provider against the same corrections:

```bash
hermes-inbox eval --compare anthropic,ollama
```

Compare **recall**. That is what the harness is for: it turns "is the cheap model
good enough" into a number instead of an argument.

Both providers score the same examples, so the table ends with an exact McNemar
test on the examples where they disagree. `p < 0.05` is a real difference;
anything above it means the two are not distinguishable on this many labels
yet — which, at 30 labels, is the usual answer when the gap is a few points.

## Do you need NemoClaw for this?

Not for what is here. The blast radius of this agent is "sends you a Telegram
message you didn't need" — it has read-only credentials and no outbound path.
The sandbox would not be earning its cost yet.

It starts earning it the moment the agent gets a write scope: drafting replies
into Gmail, sending them, or executing skills it wrote itself. Worth noting for
that day — **the Gmail scopes do not give you a safe middle ground.**
`gmail.compose` covers creating drafts *and* sending them; there is no scope
combination that reads mail and writes drafts while being structurally unable to
send. At that point "don't send" stops being a property of the credential and
becomes a policy guarantee — which is exactly the argument ADR 0001 makes for
putting enforcement below the agent.

So: ship this on read-only, and let the sandbox arrive with the write scope that
needs it.

## Swapping the pieces

Each boundary is a protocol with more than one implementation already, which is
the only real proof that a seam works:

| Seam | Interface | Implementations |
|---|---|---|
| Mail source | `sources/base.py::MailSource` | `ImapSource`, `FixtureSource` |
| Notifier | `notify/base.py::Notifier` | `TelegramNotifier`, `ConsoleNotifier` |
| Classifier | `providers.py::resolve` | Anthropic, Ollama, OpenAI-compatible (vLLM, NIM), offline heuristics |

**WhatsApp instead of Telegram:** implement `send` and `poll_feedback` behind
`Notifier` and nothing upstream changes. Telegram is first only because a bot
token takes two minutes from @BotFather, where WhatsApp's Business API needs a
Meta business account, a dedicated number, and template approval before it will
deliver a business-initiated message.

**Gmail API instead of IMAP:** implement `fetch_new`. IMAP is first because an
app password needs no OAuth consent screen and works against any provider.

## Where this sits in the plan

It collapses several phases of [PLAN.md](PLAN.md) into something runnable,
against data you have rather than data you don't:

| Phase | Plan | Here |
|---|---|---|
| 1 | Ticket ingestion, `Ticket` schema, redaction | `Message`, `sources/`, `redact.py` |
| 2 | Seed skills, no auto-promotion | Corrections are proposals; you promote by labeling |
| 3 | Policy gate as code | `gate.py`, with adversarial fixture |
| 4 | Eval harness | `evals.py`, leave-one-out, intervals; golden set gated in CI |
| 5 | Shadow mode | Read-only by construction — there is no send path to gate |

Phase 0 (NemoClaw) is deliberately **not** a prerequisite here; see above.


## Phase status

**This phase is done.** What it delivers, and what it deliberately does not:

| | |
|---|---|
| ✅ Ingestion | IMAP (read-only) + fixtures behind one `MailSource` protocol |
| ✅ Redaction | Cards, phones, OTPs, API keys, URL credentials — before the model |
| ✅ Classification | Three providers behind one seam, schema-constrained output |
| ✅ Policy gate | Six deterministic rules; human rules beat the model score |
| ✅ Notification | Telegram with feedback buttons, console fallback |
| ✅ Correction loop | Button press → labeled example → next prompt |
| ✅ Eval harness | Leave-one-out replay, precision/recall |
| ❌ Reply drafting | Needs a write scope — see the NemoClaw section above |
| ❌ Sandbox | Not yet earning its cost; arrives with the write scope |
| ❌ WhatsApp | `Notifier` seam is ready; Telegram first for setup speed |

### Known limits

- **Polling, not push.** 60s latency by default. IMAP IDLE would cut that at the
  cost of a reconnect state machine.
- **A permanently unclassifiable message blocks the cursor.** On a classify
  failure the cycle stops without advancing, so an outage cannot silently
  swallow mail. The trade is that a genuinely poisonous message would stall the
  queue — loudly, in the error output, every cycle.
- **Retries are bounded.** Connection errors, 408, 429 and 5xx get
  `HERMES_HTTP_RETRIES` extra attempts with exponential backoff; other 4xx fail
  at once. A provider down longer than that stops the cycle without losing mail
  ([F-004](DECISIONS.md#f-004--a-provider-outage-would-have-discarded-the-mailbox)).
- **Corrections grow unboundedly.** `HERMES_MAX_EXAMPLES` (default 40) caps what
  reaches the prompt, newest first. There is no pruning or clustering yet.
- **No per-sender memory.** Every message is judged independently; the customer
  profile in `ARCHITECTURE.md` is not built.

### Next phase

Pick one, in rough order of value:

1. **Run it on real mail for a week.** Everything above is theory until it has
   scored your actual inbox. Collect 30+ corrections, then run `eval` — that
   number decides everything below.
2. **Per-sender memory.** The biggest accuracy win available: "this person has
   emailed me four times and I replied every time" is a stronger signal than
   anything in the message body.
3. **Reply drafting**, which is where the sandbox and Phase 0 finally become
   load-bearing.
