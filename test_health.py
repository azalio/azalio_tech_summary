"""Unit tests for health.py — pure, no network/E5, runs anywhere."""

import json
import os

import health


def test_baseline_value_uses_median_of_nonzero():
    assert health.baseline_value([4, 5, 6]) == 5.0
    # Zero-runs are excluded so an outage doesn't decay the baseline.
    assert health.baseline_value([0, 5, 5, 5]) == 5.0
    assert health.baseline_value([0, 0, 8, 10, 8]) == 8.0
    # No healthy evidence left in the window -> no baseline.
    assert health.baseline_value([0, 0, 0]) == 0.0
    assert health.baseline_value([]) == 0.0


def test_consecutive_zeros():
    assert health.consecutive_zeros([5, 5, 0, 0, 0]) == 3
    assert health.consecutive_zeros([0, 0, 5]) == 0  # last run was healthy
    assert health.consecutive_zeros([]) == 0
    assert health.consecutive_zeros([0, 0, 0]) == 3


def test_transient_zero_not_flagged():
    # A single (or short) zero streak below a full day must NOT alert — this is
    # the noisy-hourly-failure case we're suppressing.
    baselines = {"INFRA / DEVOPS / SRE": [10, 12, 11, 9]}
    assert health.detect_anomalies(baselines, {"INFRA / DEVOPS / SRE": 0}) == []
    # Even 23 consecutive zeros (one short of a day) stays quiet.
    baselines = {"INFRA / DEVOPS / SRE": [10, 12] + [0] * 22}
    assert health.detect_anomalies(baselines, {"INFRA / DEVOPS / SRE": 0}) == []


def test_silent_failure_flagged_after_full_day():
    # 23 trailing zeros in history + this run's zero = a 24-run streak -> fire.
    baselines = {"INFRA / DEVOPS / SRE": [10, 12] + [0] * 23}
    anomalies = health.detect_anomalies(baselines, {"INFRA / DEVOPS / SRE": 0})
    assert len(anomalies) == 1
    assert anomalies[0]["collector"] == "INFRA / DEVOPS / SRE"
    assert anomalies[0]["kind"] == "silent"
    assert anomalies[0]["streak"] == 24


def test_silent_failure_renags_daily_not_hourly():
    base = {"X": [10, 10]}
    # Fires at the 24th, 48th consecutive zero, but not on the in-between hours.
    fires = {
        streak
        for streak in range(1, 49)
        # history has (streak-1) trailing zeros; this run is the streak-th zero.
        if health.detect_anomalies(
            {"X": base["X"] + [0] * (streak - 1)}, {"X": 0}
        )
    }
    assert fires == {24, 48}


def test_silent_streak_threshold_configurable():
    # min_silent_streak=1 restores fire-on-first-zero (used by callers/tests).
    baselines = {"INFRA / DEVOPS / SRE": [10, 12, 11, 9]}
    anomalies = health.detect_anomalies(
        baselines, {"INFRA / DEVOPS / SRE": 0}, min_silent_streak=1
    )
    assert len(anomalies) == 1 and anomalies[0]["streak"] == 1


def test_no_flag_when_count_present():
    baselines = {"HackerNews": [20, 20, 20]}
    assert health.detect_anomalies(baselines, {"HackerNews": 18}) == []


def test_sparse_collector_not_flagged():
    # Env-gated / intrinsically sparse source sits below min_baseline -> ignored.
    baselines = {"FINNHUB MARKET NEWS": [0, 0, 1, 0]}
    assert health.detect_anomalies(baselines, {"FINNHUB MARKET NEWS": 0}) == []


def test_unknown_collector_in_run_not_flagged():
    # A collector with no history yet is never judged.
    baselines = {}
    assert health.detect_anomalies(baselines, {"NewThing": 0}) == []


def test_drop_ratio_optional():
    baselines = {"TECH NEWS": [20, 20, 20]}
    # Off by default.
    assert health.detect_anomalies(baselines, {"TECH NEWS": 3}) == []
    # On: 3 is well below 20 * 0.5.
    anomalies = health.detect_anomalies(baselines, {"TECH NEWS": 3}, drop_ratio=0.5)
    assert anomalies and anomalies[0]["kind"] == "drop"


def test_update_baselines_appends_and_caps():
    baselines = {"X": list(range(30))}
    updated = health.update_baselines(baselines, {"X": 99}, window=24)
    assert len(updated["X"]) == 24
    assert updated["X"][-1] == 99


def test_update_records_zero_for_missing_collector():
    baselines = {"X": [5, 5]}
    updated = health.update_baselines(baselines, {})  # X absent this run
    assert updated["X"][-1] == 0


def test_evaluate_roundtrip(tmp_path):
    path = os.path.join(tmp_path, "health.json")
    # Seed a healthy baseline already carrying 23 trailing zeros, so this run's
    # zero trips the full-day streak.
    seed = {"INFRA / DEVOPS / SRE": [10, 11, 9, 10] + [0] * 23}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    # This run: INFRA silently returns 0 -> 24-run streak -> alert.
    anomalies = health.evaluate(path, {"INFRA / DEVOPS / SRE": 0})[0]
    assert len(anomalies) == 1
    # Baseline file updated with the new 0 appended.
    on_disk = health.load_baselines(path)
    assert on_disk["INFRA / DEVOPS / SRE"][-1] == 0


def test_evaluate_transient_zero_silent(tmp_path):
    path = os.path.join(tmp_path, "health.json")
    seed = {"INFRA / DEVOPS / SRE": [10, 11, 9, 10]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    # One bad run after a healthy history -> no alert (the noise we suppress),
    # but the zero is still recorded so the streak can build.
    anomalies = health.evaluate(path, {"INFRA / DEVOPS / SRE": 0})[0]
    assert anomalies == []
    assert health.load_baselines(path)["INFRA / DEVOPS / SRE"][-1] == 0


def test_load_baselines_corrupt_file(tmp_path):
    path = os.path.join(tmp_path, "bad.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert health.load_baselines(path) == {}


def test_load_baselines_missing_file(tmp_path):
    assert health.load_baselines(os.path.join(tmp_path, "nope.json")) == {}


def test_format_anomalies_readable():
    out = health.format_anomalies([
        {"collector": "INFRA / DEVOPS / SRE", "baseline": 10.0, "count": 0,
         "streak": 24, "kind": "silent"},
    ])
    assert "INFRA / DEVOPS / SRE" in out and "24" in out
