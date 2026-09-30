"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .agent import Agent
from .config import Config
from .feedback import Example, FeedbackStore
from .notify.console import ConsoleNotifier
from .state import DecisionLog

from .providers import NAMES as PROVIDERS

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "inbox.json"


def _source(config: Config, use_fixtures: bool):
    if use_fixtures or not config.has_imap:
        from .sources.fixtures import FixtureSource

        return FixtureSource(FIXTURES)
    from .sources.imap import ImapSource

    return ImapSource(
        host=config.imap_host,
        port=config.imap_port,
        user=config.imap_user,
        password=config.imap_password,
        folder=config.imap_folder,
    )


def _notifier(config: Config, force_console: bool):
    if force_console or not config.has_telegram:
        return ConsoleNotifier()
    from .notify.telegram import TelegramNotifier

    return TelegramNotifier(
        config.telegram_token,
        config.telegram_chat_id,
        retries=config.http_retries,
        backoff=config.http_backoff,
    )


def _configure_logging(args, config: Config) -> None:
    from .logs import configure

    configure(
        level=getattr(args, "log_level", None) or config.log_level,
        fmt=getattr(args, "log_format", None) or config.log_format,
    )


def _resolve_provider(args, config: Config):
    """Pick the classifier, honouring --provider then HERMES_PROVIDER then auto."""
    from . import providers

    requested = getattr(args, "provider", None) or config.provider
    classify_fn, name = providers.resolve(requested)
    if name == "offline" and requested in ("auto", None):
        print(
            "! no Anthropic credential found — falling back to offline keyword rules.\n"
            "  These do NOT learn from your corrections. Set ANTHROPIC_API_KEY,\n"
            "  or use --provider ollama to run a local model for free.\n",
            file=sys.stderr,
        )
    return classify_fn, name


def _build(args) -> tuple[Agent, str]:
    config = Config.from_env()
    _configure_logging(args, config)
    if getattr(args, "threshold", None) is not None:
        config.gate.threshold = args.threshold
    classify_fn, name = _resolve_provider(args, config)
    agent = Agent(
        source=_source(config, getattr(args, "fixtures", False)),
        notifier=_notifier(config, getattr(args, "console", False)),
        config=config,
        classify_fn=classify_fn,
    )
    return agent, name


def cmd_once(args) -> int:
    from . import providers

    agent, name = _build(args)
    print(
        f"source: {agent.source.name} · notifier: {agent.notifier.name}"
        f" · {providers.describe(name, agent.config)}"
    )
    result = agent.cycle()
    print(
        f"{result.fetched} fetched · {result.notified} notified"
        f" · {result.labels_applied} correction(s) applied"
    )
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_run(args) -> int:
    from . import providers

    agent, name = _build(args)
    print(
        f"watching {agent.source.name} every {agent.config.interval}s "
        f"→ {agent.notifier.name} · {providers.describe(name, agent.config)}"
        f" (ctrl-c to stop)"
    )
    try:
        agent.run()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def cmd_demo(args) -> int:
    """End-to-end run against fixtures, no credentials needed."""
    config = Config.from_env()
    config.data_dir = Path(args.data_dir)
    _configure_logging(args, config)
    from . import providers

    classify_fn, name = _resolve_provider(args, config)
    agent = Agent(
        source=_source(config, True),
        notifier=_notifier(config, True),
        config=config,
        classify_fn=classify_fn,
    )
    print(
        f"demo · {len(agent.source.fetch_new())} fixture messages"
        f" · threshold {config.gate.threshold} · {providers.describe(name, config)}"
    )
    result = agent.cycle()
    print(f"\n{result.notified} of {result.fetched} would have interrupted you.")
    print(f"decisions logged to {agent.log.path}")
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_backfill(args) -> int:
    """Classify mail already received, so there is something to review."""
    from datetime import datetime, timedelta, timezone

    from . import providers

    agent, name = _build(args)
    since = datetime.now(timezone.utc) - timedelta(days=args.days)

    try:
        pending = [
            m
            for m in agent.source.fetch_since(since, args.limit)
            if agent.log.find(m.uid) is None
        ]
    except NotImplementedError as exc:
        print(f"! {exc}", file=sys.stderr)
        return 1
    except AttributeError:
        print(f"! {agent.source.name} cannot query by date", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"! could not read the mailbox: {exc}", file=sys.stderr)
        return 1

    if not pending:
        print(f"nothing new in the last {args.days} days — already classified")
        return 0

    # One model call per message costs real money, so say so before spending it.
    print(
        f"{len(pending)} unclassified message(s) since {since:%Y-%m-%d}"
        f" · {providers.describe(name, agent.config)}"
    )
    if not args.yes and name != "offline":
        print(f"that is {len(pending)} model call(s). continue? [y/N] ", end="", flush=True)
        if input().strip().lower() not in ("y", "yes"):
            print("aborted")
            return 0

    def progress(index, total, message, verdict):
        print(f"  [{index}/{total}] {verdict.score:.2f} {verdict.category:<13} {message.subject[:46]}")

    result = agent.backfill(
        since, limit=args.limit, on_progress=progress, concurrency=args.concurrency
    )
    print(
        f"\nclassified {result.fetched} · {result.notified} would have interrupted you"
        f"\nreview them with: hermes-inbox list --min-score 0.5"
    )
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_list(args) -> int:
    """Sorted view of what the agent has decided."""
    config = Config.from_env()
    decisions = list(DecisionLog(config.ensure_data_dir() / "decisions.jsonl").iter_all())

    if args.category:
        decisions = [d for d in decisions if d.verdict.category == args.category]
    if args.min_score is not None:
        decisions = [d for d in decisions if d.verdict.score >= args.min_score]
    if args.needs_action:
        decisions = [d for d in decisions if d.verdict.suggested_action]
    if args.unlabeled:
        labeled = {e.uid for e in FeedbackStore(config.data_dir / "feedback.jsonl").all()}
        decisions = [d for d in decisions if d.message.uid not in labeled]

    if not decisions:
        print("nothing matches — try `hermes-inbox backfill --days 30` first")
        return 0

    key = {
        "score": lambda d: -d.verdict.score,
        "date": lambda d: d.message.received_at.timestamp(),
        "sender": lambda d: d.message.sender,
    }[args.sort]
    decisions = sorted(decisions, key=key)[: args.limit]

    for decision in decisions:
        message, verdict = decision.message, decision.verdict
        flag = "▲" if decision.gate.notify else " "
        print(
            f"{flag} {verdict.score:.2f}  {verdict.category:<13} {message.sender[:28]:<28}"
            f"  {message.subject[:44]}"
        )
        print(f"      {verdict.reason[:96]}")
        if verdict.suggested_action:
            print(f"      → {verdict.suggested_action}   [uid {message.uid}]")
        else:
            print(f"      [uid {message.uid}]")
    print(f"\n{len(decisions)} shown, sorted by {args.sort}")
    print("label one with: hermes-inbox feedback <uid> important|not-important")
    return 0


def cmd_feedback(args) -> int:
    config = Config.from_env()
    data = config.ensure_data_dir()
    log = DecisionLog(data / "decisions.jsonl")
    store = FeedbackStore(data / "feedback.jsonl")

    decision = log.find(args.uid)
    if decision is None:
        print(f"no decision recorded for uid {args.uid}", file=sys.stderr)
        return 1

    label = args.verdict == "important"
    store.add(Example.from_message(decision.message, label, note=args.note or ""))
    print(f"recorded: {decision.message.subject[:60]} → {'important' if label else 'not important'}")
    important, not_important = store.counts()
    print(f"corrections so far: {important} important · {not_important} not important")
    return 0


def cmd_eval(args) -> int:
    """Replay labeled examples; optionally compare providers and gate on recall."""
    import json

    from . import evals, providers

    config = Config.from_env()
    if args.threshold is not None:
        config.gate.threshold = args.threshold

    if args.golden:
        cases = evals.golden_cases()
        corpus = f"golden set ({len(cases)} fixtures)"
    else:
        cases = evals.cases_from_store(FeedbackStore(config.ensure_data_dir() / "feedback.jsonl"))
        corpus = f"your corrections ({len(cases)})"

    if args.compare:
        requested = list(dict.fromkeys(n.strip() for n in args.compare.split(",") if n.strip()))
        unknown = [n for n in requested if n not in PROVIDERS or n == "auto"]
        if unknown or len(requested) < 2:
            print(
                f"! --compare takes two or more of: {', '.join(p for p in PROVIDERS if p != 'auto')}",
                file=sys.stderr,
            )
            return 2
    else:
        requested = [getattr(args, "provider", None) or config.provider]

    concurrency = args.concurrency if args.concurrency is not None else config.concurrency
    reports: dict[str, evals.Report] = {}
    for requested_name in requested:
        classify_fn, name = (
            _resolve_provider(args, config)
            if not args.compare
            else providers.resolve(requested_name)
        )
        if not args.json:
            print(f"scoring {corpus} against {providers.describe(name, config)}", file=sys.stderr)
        try:
            reports[name] = evals.replay(
                cases,
                config,
                classify_fn=classify_fn,
                concurrency=concurrency,
                target_recall=args.target_recall,
            )
        except Exception as exc:
            print(f"! {name} failed: {exc}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps({n: r.to_dict() for n, r in reports.items()}, indent=2))
    elif args.compare:
        print()
        print(evals.compare(reports))
    else:
        print()
        print(next(iter(reports.values())).render(verbose=args.verbose))

    # A regression gate: the point estimate, since that is what a change moves.
    if args.min_recall is not None:
        if not cases:
            print("! --min-recall with nothing to score — a gate must not pass on no data", file=sys.stderr)
            return 1
        failing = {n: r.recall for n, r in reports.items() if r.recall < args.min_recall}
        for name, recall in failing.items():
            print(f"! {name}: recall {recall:.1%} is below --min-recall {args.min_recall:.1%}", file=sys.stderr)
        if failing:
            return 1
    return 0


def cmd_doctor(args) -> int:
    """What is stopping this from running, and what to type next."""
    from . import doctor

    config = Config.from_env()
    _configure_logging(args, config)
    report = doctor.run(config, login=not args.no_login)

    print(report.render())
    print()
    if report.ready:
        print("ready to run.")
    else:
        print(f"{len(report.failures)} thing(s) to fix before it can run.")
    print(f"next: {doctor.next_step(report, config)}")
    return 0 if report.ready else 1


def cmd_secrets(args) -> int:
    """Move the three secrets out of plaintext, and say where each one is now."""
    import getpass

    from . import secrets

    usable, detail = secrets.available()

    if args.action == "status":
        print(f"keychain: {detail}\n")
        for resolution in secrets.inspect():
            if not resolution.present:
                print(f"  – {resolution.name:<20} not set")
                continue
            note = f"  (also in {', '.join(resolution.shadowed)})" if resolution.shadowed else ""
            print(f"  ✓ {resolution.name:<20} {resolution.source}{note}")
        if not usable and any(r.source == secrets.DOTENV for r in secrets.inspect()):
            print(f"\nno keychain to move them into: {detail}")
        return 0

    if not usable:
        print(f"! {detail}", file=sys.stderr)
        return 1

    if args.action == "import":
        values = secrets.dotenv_values()
        if not values:
            print("nothing to import — .env holds no secrets")
            return 0
        for name, value in values.items():
            secrets.put(name, value)
            print(f"stored {name} in the keychain")
        print(
            "\nThey are still in .env, and .env wins — delete these lines to finish:\n  "
            + "\n  ".join(values)
            + "\n\nNothing was written to .env: a tool that rewrites the file holding your"
            "\ncredentials can only ever lose them. `doctor` will keep warning until"
            "\nthose lines are gone."
        )
        return 0

    if args.name not in secrets.SECRETS:
        print(
            f"! {args.name} is not a secret. Known: {', '.join(secrets.SECRETS)}",
            file=sys.stderr,
        )
        return 1

    if args.action == "rm":
        removed = secrets.delete(args.name)
        print(f"{'removed' if removed else 'nothing stored for'} {args.name}")
        return 0

    # `set`. The value is never a command-line argument — argv lands in shell
    # history and in anyone's `ps` output.
    value = getpass.getpass(f"{args.name} ({secrets.SECRETS[args.name]}): ").strip()
    if not value:
        print("! empty, nothing stored", file=sys.stderr)
        return 1
    secrets.put(args.name, value)
    print(f"stored {args.name} in the keychain ({detail})")
    if args.name in secrets.dotenv_values():
        print(f"! {args.name} is still in .env, and .env wins — delete that line")
    return 0


def cmd_stats(args) -> int:
    config = Config.from_env()
    data = config.ensure_data_dir()
    decisions = list(DecisionLog(data / "decisions.jsonl").iter_all())
    important, not_important = FeedbackStore(data / "feedback.jsonl").counts()

    if not decisions:
        print("nothing processed yet — try `hermes-inbox demo`")
        return 0

    notified = sum(1 for d in decisions if d.gate.notify)
    print(f"{len(decisions)} messages processed · {notified} notified ({notified/len(decisions):.0%})")
    print(f"corrections: {important} important · {not_important} not important")

    by_category: dict[str, int] = {}
    for decision in decisions:
        by_category[decision.verdict.category] = by_category.get(decision.verdict.category, 0) + 1
    print("\nby category:")
    for category, count in sorted(by_category.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {category}")

    by_rule: dict[str, int] = {}
    for decision in decisions:
        by_rule[decision.gate.rule] = by_rule.get(decision.gate.rule, 0) + 1
    print("\nby gate rule:")
    for rule, count in sorted(by_rule.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4d}  {rule}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-inbox",
        description="Watch a mailbox and interrupt you only when it matters.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run against bundled fixtures (no credentials)")
    demo.add_argument("--data-dir", default="data/demo")
    demo.add_argument("--provider", choices=PROVIDERS, help="classifier to use")
    demo.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    demo.add_argument("--log-format", choices=["text", "json"])
    demo.set_defaults(func=cmd_demo)

    once = sub.add_parser("once", help="one polling cycle, then exit")
    once.add_argument("--fixtures", action="store_true", help="force the fixture mailbox")
    once.add_argument("--console", action="store_true", help="force console output")
    once.add_argument("--threshold", type=float, help="override the notify threshold")
    once.add_argument("--provider", choices=PROVIDERS)
    once.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    once.add_argument("--log-format", choices=["text", "json"])
    once.set_defaults(func=cmd_once)

    run = sub.add_parser("run", help="poll continuously")
    run.add_argument("--fixtures", action="store_true")
    run.add_argument("--console", action="store_true")
    run.add_argument("--threshold", type=float)
    run.add_argument("--provider", choices=PROVIDERS)
    run.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    run.add_argument("--log-format", choices=["text", "json"])
    run.set_defaults(func=cmd_run)

    feedback = sub.add_parser("feedback", help="correct a call the agent made")
    feedback.add_argument("uid", help="message uid, shown in the notification")
    feedback.add_argument("verdict", choices=["important", "not-important"])
    feedback.add_argument("--note", help="why — included in the prompt as guidance")
    feedback.set_defaults(func=cmd_feedback)

    ev = sub.add_parser("eval", help="replay your corrections and score the classifier")
    ev.add_argument("--threshold", type=float)
    ev.add_argument("--provider", choices=PROVIDERS, help="classifier to score")
    ev.add_argument(
        "--compare", metavar="A,B,...", help="score several providers on the same cases, paired"
    )
    ev.add_argument(
        "--golden", action="store_true", help="replay the labeled fixture set instead of your corrections"
    )
    ev.add_argument(
        "--target-recall", type=float, default=0.95, help="recall the suggested threshold must reach"
    )
    ev.add_argument("--min-recall", type=float, help="exit 1 if recall is below this (for CI)")
    ev.add_argument("--concurrency", type=int, help="parallel calls (default HERMES_CONCURRENCY)")
    ev.add_argument("--json", action="store_true", help="machine-readable output")
    ev.add_argument("-v", "--verbose", action="store_true", help="reliability table and threshold sweep")
    ev.set_defaults(func=cmd_eval)

    backfill = sub.add_parser("backfill", help="classify mail already received (does not notify)")
    backfill.add_argument("--days", type=int, default=30, help="how far back to reach")
    backfill.add_argument("--limit", type=int, default=500, help="cap on messages fetched")
    backfill.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    backfill.add_argument(
        "--concurrency", type=int, help="parallel classifications (default 4; 1 is serial)"
    )
    backfill.add_argument("--fixtures", action="store_true")
    backfill.add_argument("--console", action="store_true")
    backfill.add_argument("--threshold", type=float)
    backfill.add_argument("--provider", choices=PROVIDERS)
    backfill.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    backfill.add_argument("--log-format", choices=["text", "json"])
    backfill.set_defaults(func=cmd_backfill)

    listing = sub.add_parser("list", help="sorted list of decisions, with summary and action")
    listing.add_argument("--sort", choices=["score", "date", "sender"], default="score")
    listing.add_argument("--limit", type=int, default=40)
    listing.add_argument("--category")
    listing.add_argument("--min-score", type=float)
    listing.add_argument("--needs-action", action="store_true", help="only those with a suggested action")
    listing.add_argument("--unlabeled", action="store_true", help="hide ones you already corrected")
    listing.set_defaults(func=cmd_list)

    doc = sub.add_parser("doctor", help="check the setup and say what to do next")
    doc.add_argument("--no-login", action="store_true", help="skip the IMAP login attempt")
    doc.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    doc.add_argument("--log-format", choices=["text", "json"])
    doc.set_defaults(func=cmd_doctor)

    sec = sub.add_parser("secrets", help="store the three secrets in the OS keychain")
    sec.add_argument(
        "action",
        nargs="?",
        default="status",
        choices=["status", "set", "rm", "import"],
        help="status (default), set, rm, or import the ones already in .env",
    )
    # Deliberately no argument for the value itself — `set` prompts for it.
    sec.add_argument("name", nargs="?", help="ANTHROPIC_API_KEY | IMAP_PASSWORD | TELEGRAM_BOT_TOKEN")
    sec.set_defaults(func=cmd_secrets)

    stats = sub.add_parser("stats", help="summarize what it has done so far")
    stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
