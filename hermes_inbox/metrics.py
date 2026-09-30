"""Statistics for the eval harness.

Pure functions, standard library only. They exist because a point estimate on a
few dozen labels is not a measurement: "recall 100%" on 4 important emails is
equally consistent with a classifier that misses one in two.

| Question | Tool | Why this one |
|---|---|---|
| How sure is this rate? | Wilson score interval | Well-behaved at 0/n and n/n, where the textbook normal interval collapses to zero width |
| Does the score mean what it says? | Brier score, expected calibration error | The gate thresholds the score, so a miscalibrated score moves the threshold's meaning |
| Where should the threshold be? | Sweep every distinct score | The operating point is a recall target, not a default someone typed |
| Is provider A really better than B? | Exact McNemar test | Both are scored on the *same* examples; an unpaired comparison throws that away |
| How slow is it? | Nearest-rank percentiles | p95 is what a backlog feels like; the mean hides it |
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

#: z for a two-sided 95% interval.
Z95 = 1.959963984540054


@dataclass(frozen=True)
class Interval:
    """A proportion with its confidence interval. `n == 0` means undefined."""

    k: int
    n: int
    low: float
    high: float

    @property
    def value(self) -> float:
        return self.k / self.n if self.n else 0.0

    @property
    def defined(self) -> bool:
        return self.n > 0

    def render(self) -> str:
        if not self.n:
            return "   n/a"
        return f"{self.value:6.1%}  [{self.low:5.1%}, {self.high:5.1%}]  {self.k}/{self.n}"


def wilson(k: int, n: int, z: float = Z95) -> Interval:
    """Wilson score interval for k successes out of n.

    Chosen over the normal approximation because the normal interval for 4/4 is
    [100%, 100%] — a claim of certainty from four samples. Wilson gives
    [51%, 100%], which is the honest answer.
    """
    if n <= 0:
        return Interval(k=0, n=0, low=0.0, high=1.0)
    if not 0 <= k <= n:
        raise ValueError(f"need 0 <= k <= n, got k={k} n={n}")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return Interval(k=k, n=n, low=max(0.0, centre - half), high=min(1.0, centre + half))


def brier(labels: Sequence[bool], scores: Sequence[float]) -> float:
    """Mean squared error of the score as a probability. 0 is perfect, 0.25 is a coin."""
    if not labels:
        return 0.0
    return sum((s - float(y)) ** 2 for y, s in zip(labels, scores)) / len(labels)


@dataclass(frozen=True)
class Bin:
    low: float
    high: float
    count: int
    mean_score: float
    positive_rate: float


def reliability(labels: Sequence[bool], scores: Sequence[float], bins: int = 5) -> list[Bin]:
    """Equal-width bins over [0, 1]: what the model said versus what was true.

    Five bins rather than the usual ten, because at 30 labels ten bins are mostly
    empty or singletons and the table stops saying anything.
    """
    buckets: list[list[tuple[bool, float]]] = [[] for _ in range(bins)]
    for y, s in zip(labels, scores):
        index = min(int(s * bins), bins - 1)  # a score of exactly 1.0 lands in the top bin
        buckets[index].append((y, s))
    out = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        out.append(
            Bin(
                low=index / bins,
                high=(index + 1) / bins,
                count=len(bucket),
                mean_score=sum(s for _, s in bucket) / len(bucket),
                positive_rate=sum(1 for y, _ in bucket if y) / len(bucket),
            )
        )
    return out


def ece(labels: Sequence[bool], scores: Sequence[float], bins: int = 5) -> float:
    """Expected calibration error: count-weighted |said − true| over the bins."""
    total = len(labels)
    if not total:
        return 0.0
    return sum(
        b.count / total * abs(b.mean_score - b.positive_rate)
        for b in reliability(labels, scores, bins)
    )


@dataclass(frozen=True)
class OperatingPoint:
    threshold: float
    precision: float
    recall: float
    alerts: int  # how many would have pinged


def sweep(labels: Sequence[bool], scores: Sequence[float]) -> list[OperatingPoint]:
    """Precision and recall at every threshold that changes the outcome.

    The candidates are the distinct scores themselves: between two adjacent
    scores every threshold behaves identically, so nothing else needs trying.
    Sorted from strictest to loosest.
    """
    positives = sum(1 for y in labels if y)
    points = []
    for threshold in sorted(set(scores), reverse=True):
        tp = sum(1 for y, s in zip(labels, scores) if s >= threshold and y)
        alerts = sum(1 for s in scores if s >= threshold)
        points.append(
            OperatingPoint(
                threshold=threshold,
                precision=tp / alerts if alerts else 0.0,
                recall=tp / positives if positives else 0.0,
                alerts=alerts,
            )
        )
    return points


def recommend_threshold(
    labels: Sequence[bool], scores: Sequence[float], target_recall: float
) -> OperatingPoint | None:
    """The strictest threshold that still reaches `target_recall`.

    Strictest, because among the thresholds that catch enough, the highest one
    sends the fewest pings. None when there are no positive labels to recall.
    """
    if not any(labels):
        return None
    for point in sweep(labels, scores):
        if point.recall >= target_recall:
            return point
    return None  # unreachable: the loosest threshold always has recall 1.0


@dataclass(frozen=True)
class McNemar:
    """Paired comparison of two classifiers on the same examples."""

    only_a: int  # A right, B wrong
    only_b: int  # B right, A wrong
    p_value: float

    def render(self, a: str, b: str) -> str:
        if not self.only_a and not self.only_b:
            return f"{a} and {b} agree on every example — no difference to test"
        verdict = "a real difference" if self.p_value < 0.05 else "not distinguishable yet"
        return (
            f"{a} right where {b} wrong: {self.only_a} · the reverse: {self.only_b}"
            f" · p = {self.p_value:.3f} ({verdict})"
        )


def mcnemar(a_correct: Sequence[bool], b_correct: Sequence[bool]) -> McNemar:
    """Exact (binomial) McNemar test, two-sided.

    Only the discordant pairs carry information: an example both get right or
    both get wrong says nothing about which is better. The exact form is used
    because the chi-squared approximation needs ~25 discordant pairs, which a
    personal inbox will not have.
    """
    if len(a_correct) != len(b_correct):
        raise ValueError("McNemar needs both classifiers scored on the same examples")
    only_a = sum(1 for x, y in zip(a_correct, b_correct) if x and not y)
    only_b = sum(1 for x, y in zip(a_correct, b_correct) if y and not x)
    n = only_a + only_b
    if n == 0:
        return McNemar(0, 0, 1.0)
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2**n
    return McNemar(only_a, only_b, min(1.0, 2 * tail))


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile, q in [0, 100]. Returns an observed value, never an interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]
