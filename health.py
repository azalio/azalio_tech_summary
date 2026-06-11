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

# How many recent runs to keep per collector for the rolling baseline. ~48h of
# hourly runs — wide enough that a full day of zero-output (the silent-streak
# threshold below) doesn't roll the healthy pre-failure history out of the
# window, so the baseline stays meaningful while a source is down.
HISTORY_WINDOW = 48

# A collector is only eligible for a silent-failure alert once its baseline
# (median of recent non-failing runs) is at least this high. Guards against
# noise: env-gated collectors (no API key) and intrinsically sparse ones sit at
# baseline 0 and are never flagged for returning 0.
DEFAULT_MIN_BASELINE = 3

# How many *consecutive* zero-output runs a collector must rack up before it's
# flagged as a silent failure. One stalled fetch or a genuinely quiet hour
# zeroes a feed transiently; many feeds (arXiv, RSS) hiccup for an hour or two
# and recover. At hourly cron this is ~one full day of silence — "wait a day and
# watch" before crying wolf. Once tripped, the alert re-nags once per
# min_silent_streak (i.e. daily) so a persistent outage isn't forgotten, rather
# than every single hour.
DEFAULT_MIN_SILENT_STREAK = 24

# A collector whose *healthy* history already contains at least this fraction of
# zero-output runs is treated as intermittent-by-design, not broken, and is
# exempt from the silent-failure alert. Some sources are bursty: a single
# once-daily arXiv batch feed (e.g. ARXIV ASTROPHYSICS = one rss/astro-ph feed
# capped at a few items/run) is fully consumed within hours of the daily mailing,
# then correctly yields 0 for the rest of the day — the feed is healthy, every
# entry is just already-seen — until tomorrow's batch, easily 24h+ of zeros over
# nights and weekends. A steady hourly feed (HN, Reddit, news RSS) is non-zero
# every run when alive, so a genuine death still trips the alert. Self-tuning: no
# per-source label list to maintain. Trade-off: a real death of an intermittent
# source is suppressed too (acceptable — these are niche garnish sources, and the
# cost of daily false alerts is alert fatigue that masks real ones).
DEFAULT_INTERMITTENT_ZERO_FRAC = 0.2

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
    """Representative *healthy* volume for a collector: median of its non-zero runs.

    Median (not mean) so one outlier run doesn't skew it. Zero-runs are excluded
    so a long outage doesn't decay the baseline toward 0 and mask the very
    failure we want to report — the baseline holds at "what this source yields
    when it works" until the entire window is zeros (no healthy evidence left, at
    which point returning 0 is no longer surprising and we stop alerting).
    """
    vals = [v for v in history if v]  # drop None and 0
    if not vals:
        return 0.0
    return float(median(vals))


def consecutive_zeros(history: list) -> int:
    """Length of the run of zero-output runs at the tail of ``history`` — i.e.
    how long the current silent streak is, not counting the run about to happen."""
    n = 0
    for v in reversed(history):
        if v == 0:
            n += 1
        else:
            break
    return n


def is_intermittent(history: list, trailing_zeros: int, frac: float) -> bool:
    """True if zero-output is a normal feature of this source, not a failure.

    Looks only at the *healthy* history before the current zero-streak (so an
    in-progress outage doesn't make a steady feed look intermittent) and asks
    whether zeros already made up at least ``frac`` of it. A once-daily batch
    feed flatlines at 0 for much of every day by design; a steady hourly feed
    does not. See ``DEFAULT_INTERMITTENT_ZERO_FRAC``.
    """
    healthy = history[: len(history) - trailing_zeros]
    if not healthy:
        return False
    return healthy.count(0) / len(healthy) >= frac


def detect_anomalies(
    baselines: dict,
    run_counts: dict,
    *,
    min_baseline: int = DEFAULT_MIN_BASELINE,
    min_silent_streak: int = DEFAULT_MIN_SILENT_STREAK,
    intermittent_zero_frac: float = DEFAULT_INTERMITTENT_ZERO_FRAC,
    drop_ratio: float = DEFAULT_DROP_RATIO,
) -> list:
    """Compare this run's per-collector counts against rolling baselines.

    Returns a list of anomaly dicts (sorted worst-first) with keys:
        collector, baseline, count, kind ("silent" | "drop"), and — for the
        "silent" kind — streak (consecutive zero-runs including this one).

    A collector is flagged "silent" only once it has returned 0 for
    ``min_silent_streak`` consecutive runs (so transient single-run dips never
    alert), and then again every ``min_silent_streak`` runs while it stays down
    (a daily re-nag, not an hourly one). Sources whose healthy history is already
    rich in zero-runs are intermittent-by-design (bursty batch feeds) and are
    exempt — see :func:`is_intermittent`.

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
            # Streak including this run = trailing zeros in history so far + 1
            # (detection runs before this run is folded into the baselines).
            trailing = consecutive_zeros(history)
            streak = trailing + 1
            if is_intermittent(history, trailing, intermittent_zero_frac):
                continue  # bursty by nature; long zero-runs are normal for it
            # Fire at the first full-day streak, then once per day thereafter.
            if streak % min_silent_streak == 0:
                anomalies.append({
                    "collector": collector,
                    "baseline": base,
                    "count": 0,
                    "streak": streak,
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
            streak = a.get("streak", 0)
            lines.append(
                f"⚠️ {a['collector']}: молчит {streak} запусков подряд "
                f"(≈{streak}ч, обычно ~{a['baseline']:.0f}) — источник, похоже, мёртв"
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
    min_silent_streak: int = DEFAULT_MIN_SILENT_STREAK,
    intermittent_zero_frac: float = DEFAULT_INTERMITTENT_ZERO_FRAC,
    drop_ratio: float = DEFAULT_DROP_RATIO,
) -> tuple:
    """One-call helper: load -> detect -> persist updated baselines.

    Returns ``(anomalies, updated_baselines)``. Detection runs against the
    *previous* baselines (before this run is folded in), so a real failure is
    judged against history, not against itself — and the silent-streak count
    sees the trailing zeros that led up to this run.
    """
    baselines = load_baselines(path)
    anomalies = detect_anomalies(
        baselines,
        run_counts,
        min_baseline=min_baseline,
        min_silent_streak=min_silent_streak,
        intermittent_zero_frac=intermittent_zero_frac,
        drop_ratio=drop_ratio,
    )
    updated = update_baselines(baselines, run_counts)
    save_baselines(path, updated)
    return anomalies, updated
