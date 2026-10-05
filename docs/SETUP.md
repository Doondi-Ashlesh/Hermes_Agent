# Setup

From a clean machine to a running agent. Every step has a **verify** you should
see before moving on — if a step's output differs, stop there rather than
continuing and debugging two problems at once.

**Time:** ~5 minutes offline, ~20 with a real mailbox and Telegram.

## Prerequisites

| | Version | Check |
|---|---|---|
| Python | 3.10+ | `python3 --version` |
| git | any | `git --version` |
| make | optional | `make --version` |

Nothing else. No Docker, no GPU, no NemoClaw — see
[D-005](DECISIONS.md#d-005--ship-outside-the-nemoclaw-sandbox-for-now) for why
the sandbox is not part of this yet.

---

## Step 1 — Install

```bash
git clone https://github.com/Doondi-Ashlesh/Hermes_Agent.git
cd Hermes_Agent
make install
```

Without `make`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

**Verify** — the suite passes with no network and no credentials:

```bash
make test
```

```
313 passed in 0.86s
```

If this fails on a clean clone, that is a bug in the repo, not in your machine.

---

## Step 2 — See it work offline

```bash
make demo
```

**Verify** — 12 fixture emails go through the real pipeline and 4 are flagged:

```
demo · 12 fixture messages · threshold 0.7 · offline keyword rules
▲ Re: Quick intro — inventory sync        personal · 0.80 · uid 102
▲ Payment failed for invoice INV-90233    billing  · 0.90 · uid 103
▲ New sign-in from an unrecognized device security · 0.95 · uid 105
▲ Contract redlines — sign-off by Thursday personal · 0.80 · uid 110

4 of 12 would have interrupted you.
```

Nothing was flagged for the newsletter, the marketing blast, the bounce, or the
message that tries to instruct the classifier (uid 109 — deliberate, see
[Safety](INBOX_AGENT.md#safety-properties)).

That run used keyword rules, not a model. Everything below replaces them.

---

## Step 3 — Configure

At any point from here, `hermes-inbox doctor` tells you what is still missing
and the one command to run next. It reads only — it never writes config, sends
anything, or prints a secret.

```
  ✓ .env            found
  ✗ mailbox         not configured — missing IMAP_PASSWORD
                      → Gmail needs a 16-char App Password (SETUP.md §3b)
  ! notifier        telegram not configured — alerts will print to the console

  1 thing(s) to fix before it can run.
  next: Gmail needs a 16-char App Password (SETUP.md §3b)
```

```bash
cp .env.example .env
```

`.env` is gitignored. Fill in the three sections below.

### 3a — Model access

Pick one:

**Hosted (recommended).** Get a key from `console.anthropic.com`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
HERMES_MODEL=claude-sonnet-5
```

Roughly $12–18/month at 100 emails/day — the arithmetic, and why not Haiku, is
in [What it costs](INBOX_AGENT.md#what-it-costs).

**Local and free.** Install [Ollama](https://ollama.com), then:

```bash
ollama serve &
ollama pull qwen2.5:7b
```

```bash
HERMES_PROVIDER=ollama
HERMES_OLLAMA_MODEL=qwen2.5:7b
```

Nothing leaves your machine. It follows corrections less reliably — measure it
with `make eval` rather than guessing (Step 6).

**Self-hosted on a GPU.** Any OpenAI-compatible server, such as vLLM or an NVIDIA
NIM container:

```bash
HERMES_PROVIDER=openai-compat
HERMES_OPENAI_BASE_URL=http://localhost:8000/v1
```

Leave `HERMES_OPENAI_MODEL` empty to use whatever the server is serving.

**Verify:**

```bash
.venv/bin/hermes-inbox demo
```

The banner must name your provider, not `offline keyword rules`:

```
demo · 12 fixture messages · threshold 0.7 · anthropic · claude-sonnet-5
```

Still seeing `offline`? The key isn't being read. `HERMES_PROVIDER=auto` falls
back silently by design — force it with `--provider anthropic` to see the error.

### 3b — Mailbox (read-only)

**Gmail** needs an app password, not your account password:

1. Turn on 2-Step Verification — `myaccount.google.com/security`
2. Create an app password — `myaccount.google.com/apppasswords`
3. Copy the 16-character value

```bash
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=you@gmail.com
IMAP_PASSWORD=abcd efgh ijkl mnop
IMAP_FOLDER=INBOX
```

Other providers: `imap.fastmail.com`, `outlook.office365.com`,
`127.0.0.1:1143` for Proton Bridge. Any IMAP server works.

The mailbox is opened `readonly=True` and bodies fetched with `BODY.PEEK[]`, so
this **cannot mark your mail as read or alter it in any way**
([D-006](DECISIONS.md#d-006--read-only-by-construction-not-by-policy)).

**Verify:**

```bash
.venv/bin/hermes-inbox once --console
```

```
source: imap · notifier: console · anthropic · claude-sonnet-5
12 fetched · 3 notified · 0 correction(s) applied
```

### 3c — Telegram

1. Message [@BotFather](https://t.me/botfather) → `/newbot` → follow prompts → copy the token
2. **Send your new bot any message** (it cannot message you first)
3. Get your chat id:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
  | grep -o '"chat":{"id":[0-9-]*' | head -1
```

```bash
TELEGRAM_BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=987654321
```

**Verify** — drop the threshold so something is guaranteed to fire:

```bash
.venv/bin/hermes-inbox once --fixtures --threshold 0.1
```

Four alerts should arrive on Telegram, each with **✅ Right call** and
**🔕 Not important** buttons.

`getUpdates` returning `{"ok":true,"result":[]}` means step 2 was skipped.

### 3d — Get the secrets out of plaintext (optional)

Three of the values you just wrote are secret: `ANTHROPIC_API_KEY`,
`IMAP_PASSWORD`, `TELEGRAM_BOT_TOKEN`. They are sitting in `.env` in the clear,
which is fine on a laptop you alone use and poor anywhere else.

```bash
pip install 'hermes-inbox[keyring]'
.venv/bin/hermes-inbox secrets import
```

**Verify** — each secret is reported, and none of them is printed:

```
keychain: macOS Keyring

  ✓ ANTHROPIC_API_KEY     .env  (also in keyring)
  ✓ IMAP_PASSWORD         .env  (also in keyring)
  ✓ TELEGRAM_BOT_TOKEN    .env  (also in keyring)
```

`.env` still wins, so delete those lines from `.env` to finish the move,
then run it again — the sources should read `keyring`. `import` deliberately
does not edit `.env` for you; a tool that rewrites the file holding your
credentials can only ever lose them.

No keychain on this machine — a headless server, most likely? Then skip this
step. Everything degrades to `.env`, and a systemd unit is better served by
`EnvironmentFile=` pointing at a root-owned `chmod 600` file. Details in
[Where secrets live](INBOX_AGENT.md#where-secrets-live).

---

## Step 4 — Tune the gate

Before running continuously, set the deterministic rules. These beat the model's
score, in the order listed in [The policy gate](INBOX_AGENT.md#the-policy-gate):

```bash
HERMES_THRESHOLD=0.7                          # how sure before interrupting you
HERMES_ALWAYS_SENDERS=boss@work.com,stripe.com # always ping, whatever the score
HERMES_NEVER_SENDERS=newsletters.example       # never ping, whatever the score
HERMES_MUTED_CATEGORIES=newsletter,promotion
HERMES_QUIET_HOURS=22-7                        # local time
```

Senders match a full address or a bare domain.

**Verify** — `make stats` shows which rule fired for each decision, so you can
confirm your rules are the ones deciding:

```
by gate rule:
    24  score<0.7
     6  score>=0.7
     3  never-sender:newsletters.example
     1  always-sender:stripe.com
```

---

## Step 5 — Run it

```bash
make run
```

Polls every 60 seconds. Leave it in a terminal for a day before doing anything
clever with systemd — you want to see its judgement before you trust it
unattended.

To keep it alive across reboots, `systemd --user`:

```ini
# ~/.config/systemd/user/hermes-inbox.service
[Unit]
Description=Hermes inbox agent
After=network-online.target

[Service]
WorkingDirectory=%h/Hermes_Agent
ExecStart=%h/Hermes_Agent/.venv/bin/hermes-inbox run
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now hermes-inbox
journalctl --user -u hermes-inbox -f
```

---

## Step 6 — Correct it, then measure it

This is the part that makes it improve, and the part people skip.

**Correct** — press a button on any alert, or from the terminal:

```bash
.venv/bin/hermes-inbox feedback 1043 not-important --note "vendor newsletters never matter"
```

Each correction is appended to `data/feedback.jsonl` and injected into every
later classification.

**Measure** — once you have ~30 corrections:

```bash
make eval
```

```
Replayed 31 labeled example(s) (11 important), leave-one-out, threshold 0.7.

               value   95% interval     n
  accuracy    87.1%  [71.1%, 94.9%]  27/31
  precision   90.0%  [59.6%, 98.2%]  9/10   of the pings, how many you wanted
  recall      81.8%  [52.3%, 94.9%]  9/11   of what mattered, how much it caught
```

Read the interval, not just the value: with 11 important emails labeled, 81.8%
recall means somewhere between about half and nearly all. Label more to narrow it.

Watch **recall**. A false positive is one unwanted buzz; a false negative is an
email you never saw.

Comparing providers on your own mail:

```bash
.venv/bin/hermes-inbox eval --compare anthropic,ollama
```

Same corrections, same leave-one-out method, scored side by side, with a paired
test saying whether the difference is real or noise.

---

## Troubleshooting

Run `hermes-inbox doctor` first — it identifies most of the table below
directly, including a wrong Gmail password.

| Symptom | Cause | Fix |
|---|---|---|
| Banner says `offline keyword rules` unexpectedly | No credential resolved; `auto` fell back | `--provider anthropic` to see the real error |
| `cannot reach ollama … is 'ollama serve' running?` | Ollama not started | `ollama serve &` |
| `classify failed … stopping this cycle` repeatedly | Provider down or key invalid | Cycle stops **without** losing mail; fix the provider and it resumes ([F-004](DECISIONS.md#f-004--a-provider-outage-would-have-discarded-the-mailbox)) |
| `AUTHENTICATIONFAILED` from IMAP | Using account password, not app password | Regenerate at `myaccount.google.com/apppasswords` |
| Telegram alerts never arrive | Bot never messaged first, or wrong chat id | Message the bot, re-run the `getUpdates` curl |
| Buttons do nothing | Only `run`/`once` drain them; a cycle must follow the press | Run another cycle |
| `0 fetched` forever | Cursor is past everything | `rm data/state.json` to re-scan |
| Nothing ever notifies | Threshold too high, or category muted | `make stats` → check `by gate rule` |
| Can't tell why it decided something | Default level hides per-message detail | `hermes-inbox once --log-level DEBUG` |
| Logs unreadable in `journalctl` | Text format | `HERMES_LOG_FORMAT=json`, then `journalctl -o cat \| jq` |
| Too many alerts | Threshold too low | Raise `HERMES_THRESHOLD`, correct a few, `make eval` |
| Stored a secret in the keychain, nothing changed | `.env` outranks the keychain | Delete that line from `.env`; `hermes-inbox secrets` shows which source won |
| `no OS keychain here` | Headless machine — no Keychain, no Secret Service | Expected. Keep using `.env`, or `EnvironmentFile=` in the unit |

Data lives in `data/`: `state.json` (cursor), `decisions.jsonl` (every call),
`feedback.jsonl` (your corrections). All plain text, safe to inspect. Deleting
`feedback.jsonl` erases everything it has learned.

---

## Next

- [INBOX_AGENT.md](INBOX_AGENT.md) — what it does and how the correction loop works
- [EXTENDING.md](EXTENDING.md) — add a mail source, notifier, or provider
- [DECISIONS.md](DECISIONS.md) — why it is built this way
