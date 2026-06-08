#!/usr/bin/env python3
"""Offline eval harness over the digest audit log (``digest_runs.jsonl``).

The hourly run appends one record per digest: the raw candidate ``intelligence``
(including the ArXiv / HuggingFace papers blocks), the ``event_signals``, and the
final posted ``summary``. This script turns that append-only log into metrics so
you can answer, over history rather than by eyeballing one run:

  * Is the applied-vs-fundamental AI/ML filter behaving? (How many ArXiv/HF paper
    candidates were offered vs how many survived into the 🤖 AI/ML section, and
    *which* papers were kept — so you can spot pure-theory leaks.)
  * How thin are digests? (no-news rate, item counts, sections present.)

Pure parsing — no network, no E5, no DB — so the parsing functions are unit
tested locally; the CLI runs on the server against the live JSONL.

Usage:
    python3 eval_digest.py [path/to/digest_runs.jsonl] [--last N] [--json]
    # gzipped rotations: zcat digest_runs.jsonl.1.gz | python3 eval_digest.py -
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Iterable


# A paper candidate line in the intelligence blob looks like:
#   "- Some Title (123 upvotes) - Link: https://huggingface.co/papers/..."
#   "[cs.LG] Some Title - Link: http://arxiv.org/abs/..."
# We key papers by their URL (arxiv/hf) since titles get reworded in the digest.
_PAPER_URL_RE = re.compile(r"(https?://[^\s]*(?:arxiv\.org|huggingface\.co/papers)[^\s]*)")
_ARXIV_ID_RE = re.compile(r"arxiv\.org/(?:abs|pdf|rss)?/?([0-9]{4}\.[0-9]{4,5})", re.IGNORECASE)
_HF_ID_RE = re.compile(r"huggingface\.co/papers/([^\s/?#]+)")

# AI/ML section header in the posted digest (matches the VIBE_PROMPT structure).
_AIML_HEADER_RE = re.compile(r"\*\*.*AI\s*/\s*ML.*\*\*", re.IGNORECASE)
# Any section header (bold line) — used to slice the AI/ML section out.
_SECTION_HEADER_RE = re.compile(r"^\s*\*\*.+\*\*\s*$")

_NO_NEWS_RE = re.compile(r"значимых новостей не зафиксировано", re.IGNORECASE)


def paper_key(url: str) -> str | None:
    """Stable identity for a paper from its URL (arxiv id or hf paper id)."""
    m = _ARXIV_ID_RE.search(url)
    if m:
        return f"arxiv:{m.group(1)}"
    m = _HF_ID_RE.search(url)
    if m:
        return f"hf:{m.group(1)}"
    return None


def extract_paper_candidates(intelligence: str) -> dict:
    """Map paper_key -> first title seen, for every arxiv/hf paper offered.

    Scans the whole intelligence blob (the ARXIV / HUGGINGFACE blocks live
    inside it) and pulls every paper URL with the title text on its line.
    """
    out: dict = {}
    for line in (intelligence or "").splitlines():
        m = _PAPER_URL_RE.search(line)
        if not m:
            continue
        key = paper_key(m.group(1))
        if not key:
            continue
        # Title = the line with the URL/markup stripped.
        title = line
        title = re.sub(r"-\s*Link:\s*https?://\S+", "", title)
        title = re.sub(r"https?://\S+", "", title)
        title = re.sub(r"^[\s\-\[\]a-zA-Z.]*\]", "", title)  # leading [cs.LG] tag
        title = title.strip(" -\t")
        out.setdefault(key, title[:160])
    return out


def extract_aiml_section(summary: str) -> str:
    """Return just the 🤖 AI/ML section body of a posted digest, or "".

    Slices from the AI/ML header to the next section header (or end).
    """
    lines = (summary or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if _AIML_HEADER_RE.search(line):
            start = i + 1
            break
    if start is None:
        return ""
    body = []
    for line in lines[start:]:
        if _SECTION_HEADER_RE.match(line):
            break
        body.append(line)
    return "\n".join(body).strip()


def papers_kept_in_summary(summary: str, candidates: dict) -> set:
    """Which candidate papers (by key) appear anywhere in the posted digest.

    Matches on the paper URL/id in the summary text — robust to the editor
    rewording titles into Russian.
    """
    kept = set()
    text = summary or ""
    found_keys = set()
    for m in _PAPER_URL_RE.finditer(text):
        k = paper_key(m.group(1))
        if k:
            found_keys.add(k)
    for k in candidates:
        if k in found_keys:
            kept.add(k)
    return kept


def count_items(summary: str) -> int:
    """Bullet count in a posted digest (lines starting with the • marker)."""
    return sum(1 for line in (summary or "").splitlines() if line.lstrip().startswith("•"))


def analyze_run(record: dict) -> dict:
    """Per-run metrics from one digest_runs.jsonl record."""
    intelligence = record.get("intelligence", "") or ""
    summary = record.get("summary", "") or ""
    candidates = extract_paper_candidates(intelligence)
    kept = papers_kept_in_summary(summary, candidates)
    no_news = bool(_NO_NEWS_RE.search(summary)) and count_items(summary) == 0
    return {
        "ts": record.get("ts", ""),
        "paper_candidates": len(candidates),
        "papers_kept": len(kept),
        "kept_titles": [candidates[k] for k in kept],
        "items": count_items(summary),
        "no_news": no_news,
        "has_aiml_section": bool(extract_aiml_section(summary)),
    }


def iter_records(lines: Iterable) -> Iterable:
    """Yield parsed JSON records from JSONL lines, skipping blanks/garbage."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def aggregate(runs: list) -> dict:
    """Roll per-run metrics into a summary report."""
    n = len(runs)
    if n == 0:
        return {"runs": 0}
    total_cand = sum(r["paper_candidates"] for r in runs)
    total_kept = sum(r["papers_kept"] for r in runs)
    return {
        "runs": n,
        "no_news_runs": sum(1 for r in runs if r["no_news"]),
        "avg_items": round(sum(r["items"] for r in runs) / n, 1),
        "aiml_section_rate": round(sum(1 for r in runs if r["has_aiml_section"]) / n, 2),
        "paper_candidates_total": total_cand,
        "papers_kept_total": total_kept,
        "paper_keep_rate": round(total_kept / total_cand, 3) if total_cand else 0.0,
    }


def _open_lines(path: str):
    if path == "-":
        return sys.stdin
    return open(path, "r", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path", nargs="?", default="workspace/memory/digest_runs.jsonl",
        help="path to digest_runs.jsonl, or '-' for stdin",
    )
    parser.add_argument("--last", type=int, default=0, help="only the last N runs")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--show-kept", action="store_true", help="list kept paper titles per run")
    args = parser.parse_args(argv)

    try:
        src = _open_lines(args.path)
    except OSError as e:
        print(f"cannot open {args.path}: {e}", file=sys.stderr)
        return 1
    try:
        runs = [analyze_run(rec) for rec in iter_records(src)]
    finally:
        if src is not sys.stdin:
            src.close()

    if args.last and args.last > 0:
        runs = runs[-args.last:]

    report = aggregate(runs)

    if args.json:
        print(json.dumps({"summary": report, "runs": runs}, ensure_ascii=False, indent=2))
        return 0

    if report["runs"] == 0:
        print("Нет записей для анализа.")
        return 0

    print(f"Проанализировано прогонов: {report['runs']}")
    print(f"  Пустых (no-news): {report['no_news_runs']}")
    print(f"  Среднее число пунктов: {report['avg_items']}")
    print(f"  Доля прогонов с секцией AI/ML: {report['aiml_section_rate']}")
    print(f"  Кандидатов-статей (ArXiv/HF): {report['paper_candidates_total']}")
    print(f"  Из них оставлено в дайджесте: {report['papers_kept_total']} "
          f"(keep rate {report['paper_keep_rate']})")
    if args.show_kept:
        print("\nОставленные статьи (проверь applied-vs-fundamental вручную):")
        for r in runs:
            for t in r["kept_titles"]:
                print(f"  [{r['ts']}] {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
