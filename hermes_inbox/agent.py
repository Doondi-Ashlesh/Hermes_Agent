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
from .logs import get_logger
from .schema import Decision
from .state import DecisionLog, State

log = get_logger(__name__)


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
                log.warning("correction for unknown message", extra={"uid": uid})
                errors.append(f"feedback for unknown message {uid}")
                continue
            log.info(
                "correction recorded",
                extra={"uid": uid, "label": "important" if is_important else "not-important"},
            )
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
        # Persist the notifier offset immediately: getUpdates deletes consumed
        # updates server-side, so a crash before saving loses those corrections.
        self.state.save()

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
                log.error(
                    "classify failed — stopping cycle without advancing the cursor",
                    extra={"uid": message.uid, "error": str(exc)},
                )
                result.errors.append(
                    f"classify failed for {message.uid} ({exc}) — stopping this cycle,"
                    f" {result.fetched - len(result.errors)} message(s) left for the next one"
                )
                break

            gate = decide(message, verdict, self.config.gate)
            decision = Decision(message=message, verdict=verdict, gate=gate)
            self.log.append(decision)
            log.debug(
                "decided",
                extra={
                    "uid": message.uid,
                    "score": verdict.score,
                    "category": verdict.category,
                    "rule": gate.rule,
                    "notify": gate.notify,
                },
            )

            if gate.notify:
                try:
                    self.notifier.send(decision)
                    result.notified += 1
                    log.info(
                        "notified",
                        extra={
                            "uid": message.uid,
                            "sender": message.sender,
                            "score": verdict.score,
                            "rule": gate.rule,
                        },
                    )
                except Exception as exc:
                    log.warning(
                        "notify failed", extra={"uid": message.uid, "error": str(exc)}
                    )
                    result.errors.append(f"notify failed for {message.uid}: {exc}")

            # Persist per message, not per cycle. Held only in memory, a crash
            # or SIGTERM mid-cycle would replay every message since the last
            # save and re-send notifications for all of them.
            self.state.set_last_uid(self.source.name, message.uid)
            self.state.save()

        return result

    def backfill(self, since, limit: int = 500, on_progress=None) -> CycleResult:
        """Classify mail already received, without notifying anyone.

        Exists because the eval harness needs ~30 corrections and the live loop
        only produces one decision per new message. Backfilling a month of
        history turns "wait a week" into "label your existing mail".

        Three deliberate differences from `cycle`:
        - nothing is notified; this is about producing decisions to review
        - the read cursor is untouched, so it cannot make the live loop skip mail
        - messages already in the decision log are skipped, so it is re-runnable
        """
        result = CycleResult()

        fetch_since = getattr(self.source, "fetch_since", None)
        if fetch_since is None:
            raise NotImplementedError(
                f"{self.source.name} cannot query by date; backfill needs fetch_since"
            )

        messages = [m for m in fetch_since(since, limit) if self.log.find(m.uid) is None]
        result.fetched = len(messages)
        examples = self.feedback.recent(self.config.max_examples)

        for index, message in enumerate(messages, 1):
            try:
                verdict = self.classify_fn(message, examples, self.config, client=self.client)
            except Exception as exc:
                log.error("classify failed during backfill", extra={"uid": message.uid, "error": str(exc)})
                result.errors.append(f"classify failed for {message.uid}: {exc}")
                break

            gate = decide(message, verdict, self.config.gate)
            self.log.append(Decision(message=message, verdict=verdict, gate=gate))
            if gate.notify:
                result.notified += 1  # counted as "would have", never sent
            if on_progress:
                on_progress(index, len(messages), message, verdict)

        log.info(
            "backfill complete",
            extra={"classified": result.fetched, "would_have_notified": result.notified},
        )
        return result

    def run(self, interval: int | None = None, max_cycles: int | None = None) -> None:
        """Poll forever (or `max_cycles` times, for tests)."""
        interval = self.config.interval if interval is None else interval
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            started = time.monotonic()
            result = self.cycle()
            if result.fetched or result.notified or result.labels_applied:
                log.info(
                    "cycle complete",
                    extra={
                        "fetched": result.fetched,
                        "notified": result.notified,
                        "corrections": result.labels_applied,
                        "took": round(time.monotonic() - started, 2),
                    },
                )
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
