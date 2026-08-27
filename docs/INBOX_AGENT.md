# Inbox agent — the deployable use case

The support-bot use case in [`README.md`](../README.md) needs production ticket
data to be worth anything. This one needs a mailbox you already own, which makes
it the first thing in this repo you can actually run.

It is the same architecture. [ADR 0001](adr/0001-runtime-nemoclaw-hermes.md)'s
first boundary promises that "swapping Zendesk for email must not touch agent
code" — this is that swap, and every box in
[`ARCHITECTURE.md`](ARCHITECTURE.md) keeps its shape.

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

No credentials needed:

```bash
pip install -e .
hermes-inbox demo
```

That runs 12 fixture messages through the whole pipeline with a keyword
classifier and prints what would have interrupted you. Four of the twelve do.

For the real thing:

```bash
cp .env.example .env      # fill in ANTHROPIC_API_KEY + IMAP + Telegram
hermes-inbox once         # one cycle, see what it says
hermes-inbox run          # poll every 60s
```

## Commands

| Command | What it does |
|---|---|
| `hermes-inbox demo` | Fixture mailbox, console output, no credentials |
| `hermes-inbox once` | One polling cycle against your real mailbox, then exit |
| `hermes-inbox run` | Poll continuously |
| `hermes-inbox feedback <uid> important\|not-important --note "..."` | Correct a call from the terminal |
| `hermes-inbox eval` | Replay every correction and score the classifier |
| `hermes-inbox stats` | What it has processed, by category and by gate rule |

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

So it stops making a mistake because you told it not to, and the telling
persists. That is a real feedback loop, and it is also the whole of it.

### Why the eval harness is the important half

Corrections can conflict, drown each other out, or overfit to one strange week
of mail. Without a way to measure, "it's getting better" is unfalsifiable — the
exact failure mode [PLAN.md](PLAN.md) flags for the skill library.

`hermes-inbox eval` replays every stored correction back through the classifier
with **that example excluded from the prompt** (leave-one-out — scoring an
example while the answer sits in its own context measures nothing) and reports:

```
  accuracy    66.7%
  precision  100.0%   (of the pings, how many you wanted)
  recall      50.0%   (of what mattered, how much it caught)
```

Watch **recall**. A false positive is one unnecessary buzz; a false negative is
an important email you never saw. They are not equally bad.

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
- **Ticket text is data, not instructions.** The classifier prompt says so, and
  `fixtures/inbox.json` includes an adversarial message (uid 109) that tries to
  talk the agent into flagging itself as important. It scores 0.02 as spam, and
  a test asserts that.

## Choosing a provider

The classifier is a seam. Three implementations ship:

| `HERMES_PROVIDER` | Cost | Learns from corrections | Notes |
|---|---|---|---|
| `anthropic` *(default)* | ~$9–30/mo | yes | Best judgement and injection resistance |
| `ollama` | free, local | yes, less reliably | Needs `ollama serve`; nothing leaves your machine |
| `offline` | free | **no** | Keyword rules. Demos and CI only |
| `auto` | — | — | `anthropic` if a key is set, else `offline` |

```bash
HERMES_PROVIDER=ollama HERMES_OLLAMA_MODEL=qwen2.5:7b hermes-inbox once
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
hermes-inbox eval --provider anthropic
hermes-inbox eval --provider ollama
```

Compare **recall**. That is what the harness is for: it turns "is the cheap model
good enough" into a number instead of an argument.

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
| Classifier | `providers.py::resolve` | Anthropic, Ollama, offline heuristics |

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
| 4 | Eval harness | `evals.py`, leave-one-out |
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
