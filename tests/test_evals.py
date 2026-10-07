"""The eval harness's statistics, the golden set, and the eval command.

The statistics are checked against values worked out independently (textbook
Wilson bounds, hand-counted McNemar tails), not against the code's own output.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from hermes_inbox import evals, metrics
from hermes_inbox.cli import main
from hermes_inbox.config import Config
from hermes_inbox.feedback import Example, FeedbackStore
from hermes_inbox.offline import classify as offline_classify
from hermes_inbox.schema import Verdict


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("hermes_inbox.offline.has_credentials", lambda: False)


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "k,n,low,high",
    [
        (4, 4, 0.5101, 1.0),  # the golden set's recall: perfect, and still wide
        (0, 4, 0.0, 0.4899),  # symmetric at the other end
        (5, 10, 0.2366, 0.7634),  # textbook value
        (81, 263, 0.2553, 0.3662),  # Wilson (1927) worked example, as usually cited
    ],
)
def test_wilson_interval_matches_reference_values(k, n, low, high):
    interval = metrics.wilson(k, n)
    assert interval.low == pytest.approx(low, abs=1e-3)
    assert interval.high == pytest.approx(high, abs=1e-3)


def test_wilson_never_claims_certainty_from_a_small_sample():
    """The reason it is used at all: the normal interval for 4/4 has zero width."""
    assert metrics.wilson(4, 4).low < 0.6
    assert metrics.wilson(400, 400).low > 0.99


def test_wilson_is_undefined_with_no_trials():
    interval = metrics.wilson(0, 0)
    assert not interval.defined
    assert interval.render().strip() == "n/a"


def test_wilson_rejects_impossible_counts():
    with pytest.raises(ValueError):
        metrics.wilson(5, 4)


def test_brier_bounds():
    assert metrics.brier([True, False], [1.0, 0.0]) == 0.0
    assert metrics.brier([True, False], [0.5, 0.5]) == pytest.approx(0.25)
    assert metrics.brier([True, False], [0.0, 1.0]) == 1.0


def test_perfectly_calibrated_scores_have_zero_ece():
    # 0.1 bin: 1 in 10 positive. 0.9 bin: 9 in 10 positive.
    labels = [True] + [False] * 9 + [True] * 9 + [False]
    scores = [0.1] * 10 + [0.9] * 10
    assert metrics.ece(labels, scores) == pytest.approx(0.0)


def test_overconfident_scores_show_up_in_ece():
    labels = [True, False] * 5
    scores = [0.95] * 10  # says 95%, is right 50% of the time
    assert metrics.ece(labels, scores) == pytest.approx(0.45)


def test_reliability_puts_a_score_of_one_in_the_top_bin():
    bins = metrics.reliability([True], [1.0], bins=5)
    assert len(bins) == 1 and bins[0].high == 1.0


def test_sweep_runs_strictest_to_loosest_and_recall_never_falls():
    labels = [True, True, False, True, False]
    scores = [0.9, 0.6, 0.7, 0.3, 0.1]
    points = metrics.sweep(labels, scores)
    assert [p.threshold for p in points] == [0.9, 0.7, 0.6, 0.3, 0.1]
    recalls = [p.recall for p in points]
    assert recalls == sorted(recalls)
    assert points[-1].recall == 1.0 and points[-1].alerts == 5


def test_recommended_threshold_is_the_strictest_that_reaches_the_target():
    labels = [True, True, False, True, False]
    scores = [0.9, 0.6, 0.7, 0.3, 0.1]
    point = metrics.recommend_threshold(labels, scores, target_recall=0.6)
    assert point.threshold == 0.6  # 0.7 only catches 1 of 3
    assert point.recall == pytest.approx(2 / 3)

    full = metrics.recommend_threshold(labels, scores, target_recall=1.0)
    assert full.threshold == 0.3


def test_no_threshold_is_recommended_without_positives():
    assert metrics.recommend_threshold([False, False], [0.2, 0.9], 0.95) is None


def test_mcnemar_ignores_examples_both_get_right_or_wrong():
    both_right = [True] * 50
    test = metrics.mcnemar(both_right, both_right)
    assert test.p_value == 1.0
    assert "agree" in test.render("a", "b")


def test_mcnemar_exact_tail():
    # 6 discordant pairs, all favouring A: two-sided p = 2 * (1/2)^6.
    a = [True] * 6 + [True] * 10
    b = [False] * 6 + [True] * 10
    test = metrics.mcnemar(a, b)
    assert (test.only_a, test.only_b) == (6, 0)
    assert test.p_value == pytest.approx(2 / 64)
    assert "real difference" in test.render("a", "b")


def test_mcnemar_is_not_fooled_by_a_small_lead():
    # 3 vs 1 discordant: p = 2 * (C(4,0) + C(4,1)) / 16 = 0.625.
    a = [True, True, True, False]
    b = [False, False, False, True]
    test = metrics.mcnemar(a, b)
    assert test.p_value == pytest.approx(0.625)
    assert "not distinguishable" in test.render("a", "b")


def test_mcnemar_requires_paired_samples():
    with pytest.raises(ValueError, match="same examples"):
        metrics.mcnemar([True], [True, False])


def test_percentile_is_nearest_rank():
    values = [0.1, 0.2, 0.3, 0.4, 10.0]
    assert metrics.percentile(values, 50) == 0.3
    assert metrics.percentile(values, 95) == 10.0  # the tail is what p95 is for
    assert metrics.percentile([], 50) == 0.0


# --------------------------------------------------------------------------- #
# golden set
# --------------------------------------------------------------------------- #


def test_every_fixture_has_a_golden_label():
    cases = evals.golden_cases()
    assert len(cases) == 12
    assert {c.message.uid for c in cases} == {str(uid) for uid in range(101, 113)}
    assert all(c.example.note for c in cases), "every golden label should say why"


def test_the_adversarial_fixture_is_labeled_not_important():
    """uid 109 asks to be flagged; the golden set must hold the line against it."""
    case = next(c for c in evals.golden_cases() if c.message.uid == "109")
    assert case.label is False


def test_golden_set_keeps_headers_that_classifiers_use():
    """Rebuilding from stored corrections loses headers; the golden set must not."""
    case = next(c for c in evals.golden_cases() if c.message.uid == "101")
    assert "List-Unsubscribe" in case.message.headers


def test_a_fixture_without_a_label_is_an_error(tmp_path):
    messages = tmp_path / "inbox.json"
    labels = tmp_path / "labels.json"
    messages.write_text(json.dumps([{"uid": "1", "sender": "a@b", "subject": "s", "body": "b"}]))
    labels.write_text(json.dumps({"2": {"label": True, "note": "n"}}))
    with pytest.raises(ValueError, match="unlabeled \\['1'\\]"):
        evals.golden_cases(messages, labels)


def test_offline_baseline_on_the_golden_set():
    """The baseline recorded in INBOX_AGENT.md. If this moves, update the doc."""
    report = evals.replay(evals.golden_cases(), Config(), classify_fn=offline_classify)
    assert (report.true_positive, report.false_positive) == (4, 0)
    assert (report.true_negative, report.false_negative) == (8, 0)
    assert report.recall_ci.low == pytest.approx(0.510, abs=1e-3)


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #


def scripted(scores: dict[str, float], delay: float = 0.0):
    def classify_fn(message, examples, config, client=None):
        if delay:
            time.sleep(delay)
        return Verdict(False, scores[message.uid], "other", "r")

    return classify_fn


def test_replay_is_leave_one_out_over_the_golden_set():
    seen: dict[str, set[str]] = {}

    def spy(message, examples, config, client=None):
        seen[message.uid] = {e.uid for e in examples}
        return Verdict(False, 0.0, "other", "r")

    cases = evals.golden_cases()
    evals.replay(cases, Config(), classify_fn=spy)
    everyone = {c.message.uid for c in cases}
    assert all(context == everyone - {uid} for uid, context in seen.items())


def test_replay_respects_max_examples():
    sizes = []

    def spy(message, examples, config, client=None):
        sizes.append(len(examples))
        return Verdict(False, 0.0, "other", "r")

    evals.replay(evals.golden_cases(), Config(max_examples=3), classify_fn=spy)
    assert set(sizes) == {3}


def test_concurrent_replay_keeps_case_order_and_matches_serial():
    cases = evals.golden_cases()
    scores = {c.message.uid: (int(c.message.uid) % 7) / 7 for c in cases}

    serial = evals.replay(cases, Config(), classify_fn=scripted(scores), concurrency=1)
    parallel = evals.replay(cases, Config(), classify_fn=scripted(scores, 0.01), concurrency=6)

    assert [o.uid for o in parallel.outcomes] == [c.message.uid for c in cases]
    assert parallel.correct() == serial.correct()
    assert parallel.to_dict()["recall"] == serial.to_dict()["recall"]


def test_concurrent_replay_is_actually_concurrent():
    cases = evals.golden_cases()
    active, peak, lock = [0], [0], threading.Lock()

    def slow(message, examples, config, client=None):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)
        with lock:
            active[0] -= 1
        return Verdict(False, 0.0, "other", "r")

    evals.replay(cases, Config(), classify_fn=slow, concurrency=4)
    # Overlap happened, and never beyond the cap. Not `== 4`: on a loaded runner
    # the first worker can finish before the fourth starts.
    assert 2 <= peak[0] <= 4


def test_integer_labels_count_the_same_as_booleans():
    as_bool = evals.Report(
        threshold=0.5, outcomes=[evals.Outcome("1", "s", "a", True, 0.9, "o", 0.0)]
    )
    as_int = evals.Report(threshold=0.5, outcomes=[evals.Outcome("1", "s", "a", 1, 0.9, "o", 0.0)])
    assert as_int.true_positive == as_bool.true_positive == 1
    assert as_int.correct() == [True] and as_int.misses == []


def test_a_failing_provider_stops_the_replay_without_draining_the_backlog():
    calls = []

    def dies_first(message, examples, config, client=None):
        calls.append(message.uid)
        if message.uid == "101":
            raise RuntimeError("provider down")
        time.sleep(0.05)
        return Verdict(False, 0.0, "other", "r")

    with pytest.raises(RuntimeError, match="provider down"):
        evals.replay(evals.golden_cases(), Config(), classify_fn=dies_first, concurrency=2)
    time.sleep(0.2)  # let any in-flight call finish before counting
    assert len(calls) < 12


def test_latency_is_measured_per_call():
    cases = evals.golden_cases()[:3]
    report = evals.replay(
        cases, Config(), classify_fn=scripted({c.message.uid: 0.5 for c in cases}, 0.02)
    )
    assert all(o.latency >= 0.015 for o in report.outcomes)
    assert report.latency(95) >= report.latency(50) > 0


def test_misses_put_missed_important_mail_first():
    outcomes = [
        evals.Outcome("1", "sale", "a@x", False, 0.95, "promotion", 0.0),
        evals.Outcome("2", "contract", "b@x", True, 0.10, "personal", 0.0),
    ]
    report = evals.Report(threshold=0.7, outcomes=outcomes)
    assert [m[0] for m in report.misses] == ["contract", "sale"]


def test_report_warns_before_recommending_a_threshold_from_too_few_positives():
    report = evals.replay(evals.golden_cases(), Config(), classify_fn=offline_classify)
    text = report.render()
    assert "95% interval" in text
    assert "fitted to noise" in text
    assert report.to_dict()["recommended_threshold"]["reliable"] is False


def test_verbose_render_includes_reliability_and_sweep():
    report = evals.replay(evals.golden_cases(), Config(), classify_fn=offline_classify)
    text = report.render(verbose=True)
    assert "reliability" in text and "pings" in text


def test_compare_runs_a_paired_test():
    cases = evals.golden_cases()
    good = evals.replay(cases, Config(), classify_fn=offline_classify)
    quiet = evals.replay(cases, Config(), classify_fn=scripted({c.message.uid: 0.0 for c in cases}))
    table = evals.compare({"offline": good, "silent": quiet})
    assert "offline" in table and "silent" in table
    # Four discordant pairs, all one way: p = 2 / 16.
    assert "p = 0.125" in table


def test_store_corpus_is_read_once_not_once_per_example(tmp_path, monkeypatch):
    store = FeedbackStore(tmp_path / "fb.jsonl")
    for uid in range(20):
        store.add(Example(str(uid), "a@x.example", "s", "s", uid % 2 == 0))

    reads = []
    original = FeedbackStore.all
    monkeypatch.setattr(FeedbackStore, "all", lambda self: reads.append(1) or original(self))
    evals.run_eval(store, Config(), classify_fn=scripted({str(u): 0.5 for u in range(20)}))
    assert len(reads) == 1


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #


def test_eval_golden_json_is_machine_readable(capsys):
    assert main(["eval", "--golden", "--provider", "offline", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    offline = data["offline"]
    assert offline["confusion"] == {"tp": 4, "fp": 0, "tn": 8, "fn": 0}
    assert offline["recall"]["n"] == 4
    assert set(offline["latency_s"]) == {"p50", "p95", "wall"}


def test_min_recall_gates_the_exit_code(capsys, monkeypatch):
    assert main(["eval", "--golden", "--provider", "offline", "--min-recall", "1.0"]) == 0

    # A stricter threshold drops recall; the gate must notice.
    assert (
        main(
            [
                "eval",
                "--golden",
                "--provider",
                "offline",
                "--threshold",
                "0.92",
                "--min-recall",
                "0.9",
            ]
        )
        == 1
    )
    assert "below --min-recall" in capsys.readouterr().err


def test_compare_needs_two_known_providers(capsys):
    assert main(["eval", "--golden", "--compare", "offline"]) == 2
    assert main(["eval", "--golden", "--compare", "offline,gpt-9"]) == 2
    assert main(["eval", "--golden", "--compare", "offline,offline"]) == 2  # one provider twice


def test_min_recall_fails_when_there_is_nothing_to_score(capsys):
    assert main(["eval", "--provider", "offline", "--min-recall", "0.5"]) == 1
    assert "no data" in capsys.readouterr().err


def test_compare_reports_a_dead_provider_and_fails(capsys, monkeypatch):
    monkeypatch.setattr("hermes_inbox.http._sleep", lambda seconds: None)
    monkeypatch.setenv("HERMES_OLLAMA_HOST", "http://127.0.0.1:9")  # discard port, loopback only
    monkeypatch.setenv("HERMES_HTTP_RETRIES", "0")
    assert main(["eval", "--golden", "--compare", "offline,ollama"]) == 1
    assert "ollama failed" in capsys.readouterr().err
