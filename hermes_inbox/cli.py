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


def _classifier(force_offline: bool):
    """The real classifier, or the heuristic stand-in when no key is available."""
    from .offline import classify as offline_classify
    from .offline import has_credentials

    if force_offline or not has_credentials():
        return offline_classify, True
    return None, False  # None → Agent uses the real classifier


def _build(args) -> tuple[Agent, bool]:
    config = Config.from_env()
    if getattr(args, "threshold", None) is not None:
        config.gate.threshold = args.threshold
    classify_fn, offline = _classifier(getattr(args, "offline", False))
    agent = Agent(
        source=_source(config, getattr(args, "fixtures", False)),
        notifier=_notifier(config, getattr(args, "console", False)),
        config=config,
        classify_fn=classify_fn,
    )
    return agent, offline


def _warn_offline(offline: bool) -> None:
    if offline:
        print(
            "! no ANTHROPIC_API_KEY — using the offline keyword classifier.\n"
            "  It does not learn from your corrections. Set a key for the real one.\n",
            file=sys.stderr,
        )


def cmd_once(args) -> int:
    agent, offline = _build(args)
    _warn_offline(offline)
    print(f"source: {agent.source.name} · notifier: {agent.notifier.name}")
    result = agent.cycle()
    print(
        f"{result.fetched} fetched · {result.notified} notified"
        f" · {result.labels_applied} correction(s) applied"
    )
    for error in result.errors:
        print(f"  ! {error}", file=sys.stderr)
    return 1 if result.errors else 0


def cmd_run(args) -> int:
    agent, offline = _build(args)
    _warn_offline(offline)
    print(
        f"watching {agent.source.name} every {agent.config.interval}s "
        f"→ {agent.notifier.name} (ctrl-c to stop)"
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
    classify_fn, offline = _classifier(args.offline)
    agent = Agent(
        source=_source(config, True),
        notifier=_notifier(config, True),
        config=config,
        classify_fn=classify_fn,
    )
    _warn_offline(offline)
    engine = "offline heuristics" if offline else config.model
    print(
        f"demo · {len(agent.source.fetch_new())} fixture messages"
        f" · threshold {config.gate.threshold} · {engine}"
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
    classify_fn, offline = _classifier(args.offline)
    _warn_offline(offline)
    store = FeedbackStore(config.ensure_data_dir() / "feedback.jsonl")
    print(run_eval(store, config, classify_fn=classify_fn).render())
    return 0


def cmd_stats(args) -> int:
    config = Config.from_env()
    data = config.ensure_data_dir()
    decisions = DecisionLog(data / "decisions.jsonl").all()
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
    demo.add_argument("--offline", action="store_true", help="force the heuristic classifier")
    demo.set_defaults(func=cmd_demo)

    once = sub.add_parser("once", help="one polling cycle, then exit")
    once.add_argument("--fixtures", action="store_true", help="force the fixture mailbox")
    once.add_argument("--console", action="store_true", help="force console output")
    once.add_argument("--threshold", type=float, help="override the notify threshold")
    once.add_argument("--offline", action="store_true")
    once.set_defaults(func=cmd_once)

    run = sub.add_parser("run", help="poll continuously")
    run.add_argument("--fixtures", action="store_true")
    run.add_argument("--console", action="store_true")
    run.add_argument("--threshold", type=float)
    run.add_argument("--offline", action="store_true")
    run.set_defaults(func=cmd_run)

    feedback = sub.add_parser("feedback", help="correct a call the agent made")
    feedback.add_argument("uid", help="message uid, shown in the notification")
    feedback.add_argument("verdict", choices=["important", "not-important"])
    feedback.add_argument("--note", help="why — included in the prompt as guidance")
    feedback.set_defaults(func=cmd_feedback)

    ev = sub.add_parser("eval", help="replay your corrections and score the classifier")
    ev.add_argument("--threshold", type=float)
    ev.add_argument("--offline", action="store_true")
    ev.set_defaults(func=cmd_eval)

    stats = sub.add_parser("stats", help="summarize what it has done so far")
    stats.set_defaults(func=cmd_stats)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
