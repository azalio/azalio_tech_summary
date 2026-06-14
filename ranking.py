"""Engagement-aware candidate ranking, fusion, and diversity caps.

This is the post-collection / pre-LLM ranking layer. Collectors emit free-text
lines AND register a structured :class:`Candidate` per item (see
``collectors.Collectors._add_candidate``). After dedup, ``main.py`` fuses those
candidates into a single ranked "priority index" that is handed to the editor as
a *ranking hint* (same role as ``event_signals`` — never as a standalone fact).

Three ideas, borrowed from mvanhorn/last30days-skill and adapted to a push
digest where cross-source duplicates are already merged by ``dedup.py``:

* **Uniform engagement signal.** Each source measures attention differently (HN
  points, Reddit score, Habr rating, HF upvotes, GitHub stars/day, Telegram
  views, CVSS). :func:`normalize_engagement` log-scales every raw metric onto a
  common 0..1 axis so the editor sees one comparable "how much traction" number.

* **Weighted reciprocal-rank fusion (RRF).** Items are ranked *within* their
  source stream, then fused with ``weight / (RRF_K + rank)`` (Cormack et al.
  2009). Because each source contributes its own top-ranked items, RRF gives
  natural cross-source diversity instead of letting one high-volume feed
  dominate purely on raw counts.

* **Hard diversity caps.** Per-author and per-source caps run as a real
  pre-filter on the index, not a polite request in the prompt.

The full collected text blob is still passed to the editor unchanged, so the
index only *re-orders attention*; it can never drop a story from the editor's
view. That keeps this layer low-risk and purely additive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Standard RRF smoothing constant (Cormack et al. 2009). Larger -> flatter
# contribution curve (rank matters less); 60 is the canonical default.
RRF_K = 60

# Per-collector trust weight applied to the RRF term. Higher = a top item from
# this source floats up faster. Defaults to 1.0 for anything unlisted. These are
# deliberately conservative (0.7..1.4) so weighting nudges rather than dictates;
# engagement + freshness still do most of the work. Keyed by the *collector*
# name (the section label passed to _add_candidate), not the per-feed source.
SOURCE_WEIGHTS = {
    # High-signal, human-curated or engagement-gated
    "HackerNews": 1.4,
    "Habr": 1.2,
    "HFPapers": 1.1,
    "GitHubTrending": 1.1,
    "AI LABS": 1.2,
    "INFRA / DEVOPS / SRE": 1.3,
    "ENGINEERING CURATED": 1.2,
    "Reddit": 1.1,
    "ClaudePlatform": 1.2,
    "NVD": 1.2,
    # Standard newswire / RSS
    "TECH NEWS": 1.0,
    "GLOBAL NEWS": 1.0,
    "SCIENCE & SPACE": 1.0,
    "ARXIV AI/ML PAPERS": 0.9,
    "ARXIV ASTROPHYSICS": 0.8,
    "Telegram": 1.0,
    # X/Twitter via the free acquisition cascade. Curated handles, but an
    # announce layer with PR noise — medium trust, like Telegram.
    "X": 1.0,
    # Lower-trust / noisier streams
    "GOOGLE NEWS": 0.8,
    "CHINA NEWS": 0.8,
    "CHINA TECH": 0.8,
    "NewsAPI": 0.8,
    "FINNHUB MARKET NEWS": 0.7,
    "MARKET NEWS": 0.7,
    "RUSSIAN NEWS FEED": 0.9,
}

# Raw engagement value that maps to ~0.8 on the normalized axis, per collector.
# Picked from each source's own collection gate / typical front-page value:
#   - Reddit gate is MIN_SCORE=1000, hot posts reach 5-50k -> scale 8000
#   - HN front page tops out a few hundred to ~1500 pts -> scale 600
#   - Habr/HF gate at >=100, strong posts 300-800 -> scale 400
#   - GitHub trending stars/day, gate >=200, hot repos 1-5k -> scale 1500
#   - Telegram views vary wildly by channel size -> scale 30000
# Unknown collectors with engagement fall back to DEFAULT_ENGAGEMENT_SCALE.
ENGAGEMENT_SCALES = {
    "Reddit": 8000.0,
    "HackerNews": 600.0,
    "Habr": 400.0,
    "HFPapers": 400.0,
    "GitHubTrending": 1500.0,
    "Telegram": 30000.0,
    # X likes+reposts: tracked handles routinely reach a few thousand, headline
    # posts 10k+. Only providers that expose counts (Bluesky, twscrape) populate
    # this; RSS/RSSHub/Nitter items have no engagement and rank on freshness/RRF.
    "X": 5000.0,
}
DEFAULT_ENGAGEMENT_SCALE = 500.0

# Composite weights for the final score. RRF carries source/rank diversity;
# engagement and freshness add the "traction" and "recency" axes on top.
W_RRF = 1.0
W_ENGAGEMENT = 0.6
W_FRESHNESS = 0.25


@dataclass
class Candidate:
    """One deduplicated news item with the signals needed to rank it.

    Attributes:
        collector: the collector/section name (key into SOURCE_WEIGHTS /
            ENGAGEMENT_SCALES), e.g. "HackerNews", "INFRA / DEVOPS / SRE".
        source: the concrete per-item source label, e.g. "Kubernetes",
            "r/devops" — used for the per-source diversity cap.
        title: headline (for the index display).
        url: canonical link.
        line: the exact rendered text line the collector emitted (so the index
            can show the same wording the full blob uses).
        engagement: raw source-native engagement (points/score/upvotes/...),
            or None when the source has no engagement signal.
        author: normalized author/handle for the per-author cap, or None.
        freshness: recency in 0..1 (1 = just published). Defaults to 0.5 when
            the collector can't date the item.
        cvss: special-cased 0..10 severity for NVD; normalized directly.
    """

    collector: str
    source: str
    title: str
    url: str
    line: str = ""
    engagement: Optional[float] = None
    author: Optional[str] = None
    freshness: float = 0.5
    cvss: Optional[float] = None
    # Filled in by rank_candidates(); kept on the object for debugging/tests.
    score: float = field(default=0.0, compare=False)
    norm_engagement: float = field(default=0.0, compare=False)


def normalize_engagement(collector: str, raw: Optional[float]) -> float:
    """Log-scale a raw engagement metric onto a common 0..1 axis.

    log1p compresses the long tail so a 50k-upvote Reddit post and a 1.5k-point
    HN story land in a comparable band instead of the former swamping everything.
    Returns 0.0 when there is no engagement signal.
    """
    if raw is None or raw <= 0:
        return 0.0
    scale = ENGAGEMENT_SCALES.get(collector, DEFAULT_ENGAGEMENT_SCALE)
    value = math.log1p(raw) / math.log1p(scale)
    return max(0.0, min(1.0, value))


def _candidate_engagement_norm(c: Candidate) -> float:
    """Normalized engagement for a candidate, treating CVSS as its own 0..10 axis."""
    if c.cvss is not None:
        return max(0.0, min(1.0, c.cvss / 10.0))
    return normalize_engagement(c.collector, c.engagement)


def _stream_rank_key(c: Candidate) -> tuple:
    """Within-source ordering: engagement first, then freshness."""
    return (-_candidate_engagement_norm(c), -c.freshness)


def _cap_per_key(candidates: list[Candidate], key_fn, max_per: int) -> list[Candidate]:
    """Keep at most ``max_per`` candidates sharing a key. Input must already be
    sorted best-first so the survivors are the highest-scored per key. A None
    key is never capped (e.g. items with no author)."""
    if max_per <= 0:
        return list(candidates)
    counts: dict = {}
    kept: list[Candidate] = []
    for c in candidates:
        key = key_fn(c)
        if key is None:
            kept.append(c)
            continue
        n = counts.get(key, 0)
        if n < max_per:
            kept.append(c)
            counts[key] = n + 1
    return kept


def rank_candidates(
    candidates: list[Candidate],
    *,
    pool_limit: int = 40,
    per_source_cap: int = 4,
    per_author_cap: int = 3,
) -> list[Candidate]:
    """Fuse, score, diversify, and truncate candidates into a ranked pool.

    1. Rank items within each collector stream (engagement, then freshness).
    2. Composite score = weighted RRF (carries source weight + rank) plus
       normalized engagement and freshness terms.
    3. Sort globally, then apply per-author and per-source hard caps.
    4. Truncate to ``pool_limit``.

    The returned list is ordered best-first with ``.score`` populated.
    """
    if not candidates:
        return []

    # 1. Per-stream native ranks.
    streams: dict[str, list[Candidate]] = {}
    for c in candidates:
        streams.setdefault(c.collector, []).append(c)

    for collector, items in streams.items():
        items.sort(key=_stream_rank_key)
        weight = SOURCE_WEIGHTS.get(collector, 1.0)
        for rank, c in enumerate(items, start=1):
            rrf = weight / (RRF_K + rank)
            c.norm_engagement = _candidate_engagement_norm(c)
            c.score = (
                W_RRF * rrf
                + W_ENGAGEMENT * c.norm_engagement
                + W_FRESHNESS * max(0.0, min(1.0, c.freshness))
            )

    # 2. Global ordering, best-first. Tie-break deterministically on title so the
    #    output is stable across runs (no Date.now/random in this pipeline).
    ordered = sorted(candidates, key=lambda c: (-c.score, c.collector, c.title))

    # 3. Diversity caps. Author cap first (a prolific author can span sources),
    #    then per-source cap.
    ordered = _cap_per_key(ordered, lambda c: c.author or None, per_author_cap)
    ordered = _cap_per_key(ordered, lambda c: c.source or c.collector, per_source_cap)

    # 4. Truncate.
    return ordered[:pool_limit]


def render_priority_index(ranked: list[Candidate], max_items: int = 40) -> str:
    """Render the ranked pool as a compact text hint for the editor prompt.

    Mirrors the ``event_signals`` convention: explicitly a ranking signal, not a
    fact to publish. One line per item with the normalized traction band so the
    editor can weigh attention without re-deriving it.
    """
    if not ranked:
        return "Нет ранжированных кандидатов."
    lines = []
    for i, c in enumerate(ranked[:max_items], start=1):
        if c.cvss is not None:
            eng = f"CVSS {c.cvss:g}"
        elif c.engagement:
            eng = f"{int(c.engagement)} ({c.collector.split('/')[0].strip()})"
        else:
            eng = "—"
        title = " ".join((c.title or "").split())[:140]
        lines.append(
            f"{i}. score={c.score:.3f}; traction={c.norm_engagement:.2f}; "
            f"engagement={eng}; src={c.source}; {title} {c.url}".rstrip()
        )
    return "\n".join(lines)
