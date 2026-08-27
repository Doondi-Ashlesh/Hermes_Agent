"""The run loop.

    fetch → redact → classify → gate → notify → record

Polling rather than IMAP IDLE: a 60-second poll is indistinguishable from push
at human timescales, survives dropped connections without a reconnect state
machine, and is the same code path against fixtures and a live server.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .classify import classify as default_classify
from .config import Config
from .feedback import Example, FeedbackStore
from .gate import decide
from .schema import Decision
from .state import DecisionLog, State


@dataclass
class CycleResult:
    fetched: int = 0
    notified: int = 0
    labels_applied: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class Agent:
    def __init__(
        self,
        source,
        notifier,
        config: Config,
        classify_fn=None,
        client=None,
    ):
        self.source = source
        self.notifier = notifier
        self.config = config
        self.classify_fn = classify_fn or default_classify
        self.client = client

        data = config.ensure_data_dir()
        self.state = State(data / "state.json")
        self.log = DecisionLog(data / "decisions.jsonl")
        self.feedback = FeedbackStore(data / "feedback.jsonl")

    def _apply_labels(self) -> tuple[int, list[str]]:
        """Turn button presses into stored corrections."""
        errors: list[str] = []
        applied = 0
        poll = getattr(self.notifier, "poll_feedback", None)
        if poll is None:
            return 0, errors
        try:
            labels, offset = poll(self.state.telegram_offset)
        except Exception as exc:  # a notifier outage must not stop triage
            return 0, [f"feedback poll failed: {exc}"]

        for uid, is_important in labels:
            decision = self.log.find(uid)
            if decision is None:
                errors.append(f"feedback for unknown message {uid}")
                continue
            self.feedback.add(
                Example.from_message(decision.message, is_important, note="via notification button")
            )
            applied += 1

        self.state.telegram_offset = offset
        return applied, errors

    def cycle(self) -> CycleResult:
        """One full pass. Safe to call repeatedly; never reprocesses a message."""
        result = CycleResult()

        applied, errors = self._apply_labels()
        result.labels_applied = applied
        result.errors.extend(errors)

        since = self.state.last_uid(self.source.name)
        messages = self.source.fetch_new(since_uid=since)
        result.fetched = len(messages)

        examples = self.feedback.recent(self.config.max_examples)

        for message in messages:
            try:
                verdict = self.classify_fn(message, examples, self.config, client=self.client)
            except Exception as exc:
                # Stop the cycle rather than skipping the message. Advancing past
                # an unclassified message would mark it seen forever, so a provider
                # outage would silently swallow a whole mailbox. Leaving the
                # cursor put means the next cycle retries; the error is reported
                # every time until it clears, which is the loud failure we want.
                result.errors.append(
                    f"classify failed for {message.uid} ({exc}) — stopping this cycle,"
                    f" {result.fetched - len(result.errors)} message(s) left for the next one"
                )
                break

            gate = decide(message, verdict, self.config.gate)
            decision = Decision(message=message, verdict=verdict, gate=gate)
            self.log.append(decision)

            if gate.notify:
                try:
                    self.notifier.send(decision)
                    result.notified += 1
                except Exception as exc:
                    result.errors.append(f"notify failed for {message.uid}: {exc}")

            self.state.set_last_uid(self.source.name, message.uid)

        self.state.save()
        return result

    def run(self, interval: int | None = None, max_cycles: int | None = None) -> None:
        """Poll forever (or `max_cycles` times, for tests)."""
        interval = self.config.interval if interval is None else interval
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            started = time.monotonic()
            result = self.cycle()
            if result.fetched or result.notified or result.labels_applied:
                print(
                    f"[{time.strftime('%H:%M:%S')}] "
                    f"{result.fetched} new · {result.notified} pinged"
                    f" · {result.labels_applied} correction(s)"
                )
            for error in result.errors:
                print(f"  ! {error}")
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
