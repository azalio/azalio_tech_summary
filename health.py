"""Per-collector source-health tracking and silent-failure detection.

A push digest with ~two dozen collectors fails *quietly*: when a feed dies,
changes format, or gets blocked, its collector returns an empty string and the
only symptom is a thinner digest — no error, no alert. By the time you notice,
you've lost days of a source.

This module keeps a rolling baseline of how many items each collector usually
yields and flags a collector that returns nothing when it normally returns
plenty (the canonical silent failure). It is a direct adaptation of
last30days' ``quality_nudge`` coverage check to the push-collector setting.

Pure functions over a small JSON file; no network, no DB, no E5 — unit-testable
without the server. ``main.py`` calls :func:`evaluate`, prints/notifies on
anomalies, then persists the updated baselines.
"""

from __future__ import annotations

import json
import os
from statistics import median

# How many recent runs to keep per collector for the rolling baseline. ~24h of
# hourly runs — long enough to smooth a quiet hour, short enough to adapt when a
# source's real volume shifts.
HISTORY_WINDOW = 24

# A collector is only eligible for a silent-failure alert once its baseline
# (median of recent non-failing runs) is at least this high. Guards against
# noise: env-gated collectors (no API key) and intrinsically sparse ones sit at
# baseline 0 and are never flagged for returning 0.
DEFAULT_MIN_BASELINE = 3

# Optional secondary alert: a steep drop (not to zero) versus baseline. Off by
# default — zero-output is the high-precision signal; partial drops are noisy.
DEFAULT_DROP_RATIO = 0.0


def load_baselines(path: str) -> dict:
    """Load the per-collector history map. Missing/corrupt file -> empty (first
    run); never raises, so a bad health file can't take down the digest."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Normalize to {collector: [int, ...]}.
    out: dict = {}
    for name, hist in data.items():
        if isinstance(hist, list):
            out[name] = [int(x) for x in hist if isinstance(x, (int, float))]
    return out


def baseline_value(history: list) -> float:
    """Representative recent volume for a collector: median of its history.

    Median (not mean) so a single zero-output run doesn't drag the baseline down
    enough to mask a subsequent real failure.
    """
    vals = [v for v in history if v is not None]
    if not vals:
        return 0.0
    return float(median(vals))


def detect_anomalies(
    baselines: dict,
    run_counts: dict,
    *,
    min_baseline: int = DEFAULT_MIN_BASELINE,
    drop_ratio: float = DEFAULT_DROP_RATIO,
) -> list:
    """Compare this run's per-collector counts against rolling baselines.

    Returns a list of anomaly dicts (sorted worst-first) with keys:
        collector, baseline, count, kind ("silent" | "drop").

    Only collectors already present in ``baselines`` are checked — a brand-new
    collector has no history to be judged against yet.
    """
    anomalies = []
    for collector, history in baselines.items():
        base = baseline_value(history)
        if base < min_baseline:
            continue  # too sparse / env-gated to judge
        count = int(run_counts.get(collector, 0))
        if count == 0:
            anomalies.append({
                "collector": collector,
                "baseline": base,
                "count": 0,
                "kind": "silent",
            })
        elif drop_ratio > 0 and count < base * drop_ratio:
            anomalies.append({
                "collector": collector,
                "baseline": base,
                "count": count,
                "kind": "drop",
            })
    # Worst first: silent failures before partial drops, then by baseline size.
    anomalies.sort(key=lambda a: (a["kind"] != "silent", -a["baseline"]))
    return anomalies


def update_baselines(
    baselines: dict,
    run_counts: dict,
    *,
    window: int = HISTORY_WINDOW,
) -> dict:
    """Append this run's counts to each collector's history (capped to window).

    The union of known collectors and this run's collectors is tracked, so a new
    collector starts accumulating history immediately and a collector that
    silently returns 0 still records the 0 (its baseline stays high via the
    median until failures dominate the window).
    """
    updated: dict = {k: list(v) for k, v in baselines.items()}
    names = set(updated) | set(run_counts)
    for name in names:
        hist = updated.get(name, [])
        hist.append(int(run_counts.get(name, 0)))
        if len(hist) > window:
            hist = hist[-window:]
        updated[name] = hist
    return updated


def save_baselines(path: str, baselines: dict) -> None:
    """Persist baselines. Best-effort: a write failure logs but never raises, so
    health bookkeeping can't break an already-delivered digest."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(baselines, f, ensure_ascii=False, indent=0)
    except OSError as e:
        print(f"health.save_baselines: failed to write {path}: {e}")


def format_anomalies(anomalies: list) -> str:
    """Human-readable one-liner per anomaly for logs / Telegram notice."""
    lines = []
    for a in anomalies:
        if a["kind"] == "silent":
            lines.append(
                f"⚠️ {a['collector']}: вернул 0 (обычно ~{a['baseline']:.0f}) — "
                f"вероятно, источник сломан"
            )
        else:
            lines.append(
                f"⚠️ {a['collector']}: {a['count']} против обычных "
                f"~{a['baseline']:.0f} — заметное падение"
            )
    return "\n".join(lines)


def evaluate(
    path: str,
    run_counts: dict,
    *,
    min_baseline: int = DEFAULT_MIN_BASELINE,
    drop_ratio: float = DEFAULT_DROP_RATIO,
) -> tuple:
    """One-call helper: load -> detect -> persist updated baselines.

    Returns ``(anomalies, updated_baselines)``. Detection runs against the
    *previous* baselines (before this run is folded in), so a real failure is
    judged against history, not against itself.
    """
    baselines = load_baselines(path)
    anomalies = detect_anomalies(
        baselines, run_counts, min_baseline=min_baseline, drop_ratio=drop_ratio
    )
    updated = update_baselines(baselines, run_counts)
    save_baselines(path, updated)
    return anomalies, updated
