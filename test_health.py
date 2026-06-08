"""Unit tests for health.py — pure, no network/E5, runs anywhere."""

import json
import os

import health


def test_baseline_value_uses_median():
    assert health.baseline_value([4, 5, 6]) == 5.0
    # A single zero doesn't tank the baseline (median, not mean).
    assert health.baseline_value([0, 5, 5, 5]) == 5.0
    assert health.baseline_value([]) == 0.0


def test_silent_failure_flagged():
    baselines = {"INFRA / DEVOPS / SRE": [10, 12, 11, 9]}
    anomalies = health.detect_anomalies(baselines, {"INFRA / DEVOPS / SRE": 0})
    assert len(anomalies) == 1
    assert anomalies[0]["collector"] == "INFRA / DEVOPS / SRE"
    assert anomalies[0]["kind"] == "silent"


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
    # Seed a baseline file across several runs where INFRA is healthy.
    seed = {"INFRA / DEVOPS / SRE": [10, 11, 9, 10]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    # This run: INFRA silently returns 0.
    anomalies = health.evaluate(path, {"INFRA / DEVOPS / SRE": 0})[0]
    assert len(anomalies) == 1
    # Baseline file updated with the new 0 appended.
    on_disk = health.load_baselines(path)
    assert on_disk["INFRA / DEVOPS / SRE"][-1] == 0


def test_load_baselines_corrupt_file(tmp_path):
    path = os.path.join(tmp_path, "bad.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert health.load_baselines(path) == {}


def test_load_baselines_missing_file(tmp_path):
    assert health.load_baselines(os.path.join(tmp_path, "nope.json")) == {}


def test_format_anomalies_readable():
    out = health.format_anomalies([
        {"collector": "INFRA / DEVOPS / SRE", "baseline": 10.0, "count": 0, "kind": "silent"},
    ])
    assert "INFRA / DEVOPS / SRE" in out and "0" in out
