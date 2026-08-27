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
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .feedback import FeedbackStore
from .schema import Message


@dataclass
class Report:
    total: int = 0
    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0
    misses: list[tuple[str, str, bool, float]] = None  # (subject, sender, expected, score)

    def __post_init__(self):
        if self.misses is None:
            self.misses = []

    @property
    def accuracy(self) -> float:
        if not self.total:
            return 0.0
        return (self.true_positive + self.true_negative) / self.total

    @property
    def precision(self) -> float:
        denom = self.true_positive + self.false_positive
        return self.true_positive / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.true_positive + self.false_negative
        return self.true_positive / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def render(self) -> str:
        if not self.total:
            return (
                "No labeled examples yet.\n"
                "Run the agent, correct it a few times, then re-run this."
            )
        lines = [
            f"Replayed {self.total} labeled example(s), leave-one-out.",
            "",
            f"  accuracy   {self.accuracy:6.1%}",
            f"  precision  {self.precision:6.1%}   (of the pings, how many you wanted)",
            f"  recall     {self.recall:6.1%}   (of what mattered, how much it caught)",
            f"  f1         {self.f1:6.1%}",
            "",
            f"  hits {self.true_positive}  ·  correct silences {self.true_negative}"
            f"  ·  false alarms {self.false_positive}  ·  missed {self.false_negative}",
        ]
        if self.misses:
            lines.append("")
            lines.append("Still getting these wrong:")
            for subject, sender, expected, score in self.misses[:10]:
                want = "should ping" if expected else "should stay quiet"
                lines.append(f"  [{score:.2f}] {want}: {subject[:58]}  ({sender})")
        return "\n".join(lines)


def run_eval(
    store: FeedbackStore,
    config: Config,
    classify_fn=None,
    client=None,
) -> Report:
    """Score the classifier against every stored correction."""
    from .classify import classify as default_classify

    classify_fn = classify_fn or default_classify
    examples = store.all()
    report = Report(total=len(examples))

    for example in examples:
        message = Message(
            uid=example.uid,
            source="eval",
            sender=example.sender,
            subject=example.subject,
            body=example.snippet,
            received_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        context = store.recent(config.max_examples, exclude_uid=example.uid)
        verdict = classify_fn(message, context, config, client=client)

        predicted = verdict.score >= config.gate.threshold
        if predicted and example.label:
            report.true_positive += 1
        elif predicted and not example.label:
            report.false_positive += 1
            report.misses.append((example.subject, example.sender, False, verdict.score))
        elif not predicted and example.label:
            report.false_negative += 1
            report.misses.append((example.subject, example.sender, True, verdict.score))
        else:
            report.true_negative += 1

    return report
