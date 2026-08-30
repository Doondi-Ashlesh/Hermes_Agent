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

    return TelegramNotifier(config.telegram_token, config.telegram_chat_id)


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
    from .evals import run_eval

    config = Config.from_env()
    if args.threshold is not None:
        config.gate.threshold = args.threshold
    from . import providers

    classify_fn, name = _resolve_provider(args, config)
    store = FeedbackStore(config.ensure_data_dir() / "feedback.jsonl")
    print(f"scoring against {providers.describe(name, config)}\n")
    print(run_eval(store, config, classify_fn=classify_fn).render())
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
    demo.set_defaults(func=cmd_demo)

    once = sub.add_parser("once", help="one polling cycle, then exit")
    once.add_argument("--fixtures", action="store_true", help="force the fixture mailbox")
    once.add_argument("--console", action="store_true", help="force console output")
    once.add_argument("--threshold", type=float, help="override the notify threshold")
    once.add_argument("--provider", choices=PROVIDERS)
    once.set_defaults(func=cmd_once)

    run = sub.add_parser("run", help="poll continuously")
    run.add_argument("--fixtures", action="store_true")
    run.add_argument("--console", action="store_true")
    run.add_argument("--threshold", type=float)
    run.add_argument("--provider", choices=PROVIDERS)
    run.set_defaults(func=cmd_run)

    feedback = sub.add_parser("feedback", help="correct a call the agent made")
    feedback.add_argument("uid", help="message uid, shown in the notification")
    feedback.add_argument("verdict", choices=["important", "not-important"])
    feedback.add_argument("--note", help="why — included in the prompt as guidance")
    feedback.set_defaults(func=cmd_feedback)

    ev = sub.add_parser("eval", help="replay your corrections and score the classifier")
    ev.add_argument("--threshold", type=float)
    ev.add_argument("--provider", choices=PROVIDERS, help="compare providers on the same corrections")
    ev.set_defaults(func=cmd_eval)

    stats = sub.add_parser("stats", help="summarize what it has done so far")
    stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
