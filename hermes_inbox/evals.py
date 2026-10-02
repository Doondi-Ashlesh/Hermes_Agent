"""Evaluation harness.

Replays every human-labeled example back through the classifier and scores it.
Each example is classified with itself excluded from the prompt (leave-one-out) —
otherwise the answer would be sitting in the context and every score would be a
perfect 1.0 that means nothing.

This is what keeps the correction loop honest. Corrections can conflict, drown
each other out, or overfit to one bad week of mail; without a replay score,
"it's learning" is an unfalsifiable claim.

Recall is the number to watch: a false negative is an important mail you never
saw, which is far more costly than one unnecessary ping.

A score on a few dozen labels is an estimate, so it is reported as one: every
rate carries a 95% interval, the score's calibration is measured because the
gate thresholds it, and the threshold that would hit a recall target is derived
from the data rather than assumed. The statistics live in `metrics.py`.

Two corpora can be replayed:

- **your corrections** (`feedback.jsonl`) — the real measurement
- **the golden set** (`fixtures/labels.json` over `fixtures/inbox.json`) — a
  fixed, credential-free regression check that CI runs on every push
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from . import metrics
from .config import Config
from .feedback import Example, FeedbackStore
from .schema import Message

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

#: Below this many positives a recommended threshold is fitted to noise, and
#: the report says so rather than printing it with a straight face.
MIN_POSITIVES_TO_TUNE = 10


@dataclass(frozen=True)
class Case:
    """One labeled message to replay: the full message, and its label as an example."""

    message: Message
    example: Example

    @property
    def label(self) -> bool:
        return self.example.label


@dataclass(frozen=True)
class Outcome:
    """What the classifier said about one case, and how long it took to say it."""

    uid: str
    subject: str
    sender: str
    label: bool
    score: float
    category: str
    latency: float  # seconds, wall clock for the one call


@dataclass
class Report:
    threshold: float = 0.7
    outcomes: list[Outcome] = field(default_factory=list)
    target_recall: float = 0.95
    wall: float = 0.0  # seconds for the whole replay, including concurrency

    # -- confusion matrix ------------------------------------------------------

    def _predicted(self, outcome: Outcome) -> bool:
        return outcome.score >= self.threshold

    def _count(self, predicted: bool, label: bool) -> int:
        return sum(1 for o in self.outcomes if self._predicted(o) == predicted and bool(o.label) == label)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def true_positive(self) -> int:
        return self._count(True, True)

    @property
    def false_positive(self) -> int:
        return self._count(True, False)

    @property
    def true_negative(self) -> int:
        return self._count(False, False)

    @property
    def false_negative(self) -> int:
        return self._count(False, True)

    @property
    def misses(self) -> list[tuple[str, str, bool, float]]:
        """(subject, sender, expected, score) for every wrong call, worst first."""
        wrong = [o for o in self.outcomes if self._predicted(o) != bool(o.label)]
        # Missed important mail first: that is the expensive error.
        wrong.sort(key=lambda o: (not o.label, -abs(o.score - self.threshold)))
        return [(o.subject, o.sender, o.label, o.score) for o in wrong]

    def correct(self) -> list[bool]:
        """Per-case correctness, in replay order — the input to a paired test."""
        return [self._predicted(o) == bool(o.label) for o in self.outcomes]

    # -- rates with intervals --------------------------------------------------

    @property
    def accuracy_ci(self) -> metrics.Interval:
        return metrics.wilson(self.true_positive + self.true_negative, self.total)

    @property
    def precision_ci(self) -> metrics.Interval:
        return metrics.wilson(self.true_positive, self.true_positive + self.false_positive)

    @property
    def recall_ci(self) -> metrics.Interval:
        return metrics.wilson(self.true_positive, self.true_positive + self.false_negative)

    @property
    def accuracy(self) -> float:
        return self.accuracy_ci.value

    @property
    def precision(self) -> float:
        return self.precision_ci.value

    @property
    def recall(self) -> float:
        return self.recall_ci.value

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    # -- calibration, operating point, cost ------------------------------------

    def _labels_scores(self) -> tuple[list[bool], list[float]]:
        return [o.label for o in self.outcomes], [o.score for o in self.outcomes]

    @property
    def brier(self) -> float:
        return metrics.brier(*self._labels_scores())

    @property
    def ece(self) -> float:
        return metrics.ece(*self._labels_scores())

    @property
    def recommended(self) -> metrics.OperatingPoint | None:
        return metrics.recommend_threshold(*self._labels_scores(), self.target_recall)

    @property
    def positives(self) -> int:
        return sum(1 for o in self.outcomes if o.label)

    def latency(self, q: float) -> float:
        return metrics.percentile([o.latency for o in self.outcomes], q)

    # -- output ----------------------------------------------------------------

    def to_dict(self) -> dict:
        """Machine-readable, for CI and for tracking a score across changes."""

        def interval(i: metrics.Interval) -> dict:
            return {"value": i.value, "low": i.low, "high": i.high, "k": i.k, "n": i.n}

        point = self.recommended
        return {
            "total": self.total,
            "positives": self.positives,
            "threshold": self.threshold,
            "confusion": {
                "tp": self.true_positive,
                "fp": self.false_positive,
                "tn": self.true_negative,
                "fn": self.false_negative,
            },
            "accuracy": interval(self.accuracy_ci),
            "precision": interval(self.precision_ci),
            "recall": interval(self.recall_ci),
            "f1": self.f1,
            "brier": self.brier,
            "ece": self.ece,
            "recommended_threshold": None
            if point is None
            else {
                "target_recall": self.target_recall,
                "threshold": point.threshold,
                "precision": point.precision,
                "recall": point.recall,
                "reliable": self.positives >= MIN_POSITIVES_TO_TUNE,
            },
            "latency_s": {"p50": self.latency(50), "p95": self.latency(95), "wall": self.wall},
        }

    def render(self, verbose: bool = False) -> str:
        if not self.total:
            return (
                "No labeled examples yet.\n"
                "Run the agent, correct it a few times, then re-run this."
            )
        lines = [
            f"Replayed {self.total} labeled example(s) ({self.positives} important),"
            f" leave-one-out, threshold {self.threshold:g}.",
            "",
            "               value   95% interval     n",
            f"  accuracy   {self.accuracy_ci.render()}",
            f"  precision  {self.precision_ci.render()}   of the pings, how many you wanted",
            f"  recall     {self.recall_ci.render()}   of what mattered, how much it caught",
            f"  f1         {self.f1:6.1%}",
            "",
            f"  hits {self.true_positive}  ·  correct silences {self.true_negative}"
            f"  ·  false alarms {self.false_positive}  ·  missed {self.false_negative}",
            "",
            f"  calibration  brier {self.brier:.3f} · ece {self.ece:.3f}"
            "   (0 is perfect; brier 0.25 is a coin flip)",
            f"  latency      p50 {self.latency(50) * 1000:.0f}ms · p95 {self.latency(95) * 1000:.0f}ms"
            f" · {self.total} calls in {self.wall:.2f}s",
        ]

        point = self.recommended
        if point is not None:
            lines.append(
                f"  threshold    {point.threshold:g} is the strictest reaching recall"
                f" ≥ {self.target_recall:.0%} (precision {point.precision:.0%},"
                f" {point.alerts} ping(s))"
            )
            if self.positives < MIN_POSITIVES_TO_TUNE:
                lines.append(
                    f"               only {self.positives} important example(s) —"
                    f" fitted to noise below {MIN_POSITIVES_TO_TUNE}; do not act on it yet"
                )

        if verbose:
            labels, scores = self._labels_scores()
            lines += ["", "  reliability   said   true    n"]
            for b in metrics.reliability(labels, scores):
                lines.append(
                    f"  {b.low:.1f}–{b.high:.1f}     {b.mean_score:5.2f}  {b.positive_rate:5.2f}  {b.count:3d}"
                )
            lines += ["", "  threshold  precision  recall  pings"]
            for p in metrics.sweep(labels, scores):
                lines.append(f"  {p.threshold:9.2f}  {p.precision:9.1%}  {p.recall:6.1%}  {p.alerts:5d}")

        if self.misses:
            lines.append("")
            lines.append("Still getting these wrong:")
            for subject, sender, expected, score in self.misses[:10]:
                want = "should ping" if expected else "should stay quiet"
                lines.append(f"  [{score:.2f}] {want}: {subject[:58]}  ({sender})")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# corpora
# --------------------------------------------------------------------------- #


def cases_from_store(store: FeedbackStore) -> list[Case]:
    """Your corrections, rebuilt as messages — the latest label for each, once.

    Headers were never stored, so none are replayed.
    """
    now = datetime.now(timezone.utc)
    return [
        Case(
            message=Message(
                uid=e.uid,
                source="eval",
                sender=e.sender,
                subject=e.subject,
                body=e.snippet,
                received_at=now,
            ),
            example=e,
        )
        for e in store.current()
    ]


def golden_cases(
    messages: str | Path = FIXTURES / "inbox.json",
    labels: str | Path = FIXTURES / "labels.json",
) -> list[Case]:
    """The fixture mailbox with its reference labels.

    Every fixture must carry a label and every label a fixture: a golden set that
    silently skips a message is a golden set that stopped testing it.
    """
    raw = json.loads(Path(messages).read_text(encoding="utf-8"))
    gold = json.loads(Path(labels).read_text(encoding="utf-8"))
    uids = [str(m["uid"]) for m in raw]
    if set(uids) != set(gold):
        raise ValueError(
            f"golden labels and fixtures disagree: unlabeled {sorted(set(uids) - set(gold))},"
            f" labels without a fixture {sorted(set(gold) - set(uids))}"
        )
    cases = []
    for item in raw:
        message = Message.from_dict({**item, "source": "golden"})
        entry = gold[message.uid]
        cases.append(
            Case(message=message, example=Example.from_message(message, entry["label"], entry["note"]))
        )
    return cases


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def _context(cases: Sequence[Case], exclude_uid: str, limit: int) -> list[Example]:
    """Every other example, newest `limit` — the same rule the live loop uses."""
    others = [c.example for c in cases if c.example.uid != exclude_uid]
    return others[-limit:] if limit > 0 else others


def replay(
    cases: Sequence[Case],
    config: Config,
    classify_fn=None,
    client=None,
    concurrency: int = 1,
    target_recall: float = 0.95,
) -> Report:
    """Score `classify_fn` on `cases`, each with itself left out of the prompt.

    Parallel when `concurrency > 1`. That is safe here for the same reason it is
    in backfill (D-017): no cursor moves, nothing is written, and every call is
    independent. Outcomes are kept in case order, so the per-case correctness
    lists of two providers line up for a paired test.
    """
    from .classify import classify as default_classify

    classify_fn = classify_fn or default_classify

    def score(case: Case) -> Outcome:
        context = _context(cases, case.example.uid, config.max_examples)
        started = time.perf_counter()
        verdict = classify_fn(case.message, context, config, client=client)
        elapsed = time.perf_counter() - started
        return Outcome(
            uid=case.message.uid,
            subject=case.message.subject,
            sender=case.message.sender,
            label=case.label,
            score=verdict.score,
            category=verdict.category,
            latency=elapsed,
        )

    started = time.perf_counter()
    if concurrency <= 1 or len(cases) <= 1:
        outcomes = [score(case) for case in cases]
    else:
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(max_workers=concurrency)
        futures = [pool.submit(score, case) for case in cases]
        try:
            outcomes = [future.result() for future in futures]  # case order, first error raises
        except BaseException:
            for pending in futures:
                pending.cancel()  # a dead provider costs the in-flight calls, not the backlog
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    return Report(
        threshold=config.gate.threshold,
        outcomes=outcomes,
        target_recall=target_recall,
        wall=time.perf_counter() - started,
    )


def run_eval(
    store: FeedbackStore,
    config: Config,
    classify_fn=None,
    client=None,
    concurrency: int = 1,
    target_recall: float = 0.95,
) -> Report:
    """Score the classifier against every stored correction."""
    return replay(
        cases_from_store(store),
        config,
        classify_fn=classify_fn,
        client=client,
        concurrency=concurrency,
        target_recall=target_recall,
    )


def compare(reports: dict[str, Report]) -> str:
    """Side by side on the same cases, with a paired significance test against the first."""
    names = list(reports)
    width = max(12, *(len(n) for n in names))
    lines = [
        f"  {'provider':<{width}}  {'recall':>22}  {'precision':>22}  brier   ece    p50",
    ]
    for name, r in reports.items():
        rc, pc = r.recall_ci, r.precision_ci
        lines.append(
            f"  {name:<{width}}  {rc.value:6.1%} [{rc.low:5.1%},{rc.high:6.1%}]"
            f"  {pc.value:6.1%} [{pc.low:5.1%},{pc.high:6.1%}]"
            f"  {r.brier:.3f}  {r.ece:.3f}  {r.latency(50) * 1000:4.0f}ms"
        )
    if len(names) > 1:
        lines.append("")
        base = names[0]
        for other in names[1:]:
            test = metrics.mcnemar(reports[base].correct(), reports[other].correct())
            lines.append("  " + test.render(base, other))
    return "\n".join(lines)
