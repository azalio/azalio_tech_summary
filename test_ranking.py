"""Unit tests for ranking.py — pure, no network/E5, runs anywhere."""

from ranking import Candidate, normalize_engagement, rank_candidates, render_priority_index


def test_normalize_engagement_monotonic_and_bounded():
    # More engagement -> higher normalized value, clamped to [0, 1].
    low = normalize_engagement("HackerNews", 10)
    mid = normalize_engagement("HackerNews", 300)
    high = normalize_engagement("HackerNews", 5000)
    assert 0.0 <= low < mid < high <= 1.0


def test_normalize_engagement_none_and_zero():
    assert normalize_engagement("HackerNews", None) == 0.0
    assert normalize_engagement("HackerNews", 0) == 0.0
    assert normalize_engagement("HackerNews", -5) == 0.0


def test_normalize_engagement_per_collector_scale():
    # Same raw value scores higher on a small-scale source (HN, scale 600) than
    # on a large-scale one (Reddit, scale 8000).
    raw = 500
    assert normalize_engagement("HackerNews", raw) > normalize_engagement("Reddit", raw)


def test_higher_engagement_ranks_first_same_source():
    cands = [
        Candidate("HackerNews", "Hacker News", "low", "u1", engagement=20),
        Candidate("HackerNews", "Hacker News", "high", "u2", engagement=2000),
    ]
    ranked = rank_candidates(cands, per_source_cap=10, per_author_cap=10)
    assert [c.title for c in ranked] == ["high", "low"]


def test_per_source_cap_enforced():
    cands = [
        Candidate("HackerNews", "Hacker News", f"t{i}", f"u{i}", engagement=1000 - i)
        for i in range(6)
    ]
    ranked = rank_candidates(cands, per_source_cap=2, per_author_cap=10, pool_limit=50)
    # All share source "Hacker News" -> only 2 survive.
    assert len(ranked) == 2


def test_per_author_cap_enforced():
    cands = [
        Candidate("Reddit", f"r/sub{i}", f"t{i}", f"u{i}", engagement=1000, author="bob")
        for i in range(5)
    ]
    ranked = rank_candidates(cands, per_source_cap=10, per_author_cap=2, pool_limit=50)
    assert len(ranked) == 2  # capped by author "bob"


def test_none_author_not_capped():
    cands = [
        Candidate("TECH NEWS", f"src{i}", f"t{i}", f"u{i}", freshness=0.5, author=None)
        for i in range(5)
    ]
    ranked = rank_candidates(cands, per_source_cap=10, per_author_cap=1, pool_limit=50)
    assert len(ranked) == 5  # None author is exempt from the cap


def test_pool_limit_truncates():
    cands = [
        Candidate(f"col{i}", f"src{i}", f"t{i}", f"u{i}", engagement=100)
        for i in range(30)
    ]
    ranked = rank_candidates(cands, pool_limit=10, per_source_cap=10, per_author_cap=10)
    assert len(ranked) == 10


def test_cross_source_diversity_via_rrf():
    # One spammy source with many high-engagement items vs a single item from a
    # high-trust source. RRF + per-source cap must let the other source surface.
    spam = [
        Candidate("GOOGLE NEWS", "spam", f"s{i}", f"us{i}", engagement=900)
        for i in range(10)
    ]
    infra = [Candidate("INFRA / DEVOPS / SRE", "Kubernetes", "k8s", "uk", freshness=1.0)]
    ranked = rank_candidates(spam + infra, pool_limit=5, per_source_cap=3, per_author_cap=10)
    sources = {c.source for c in ranked}
    assert "Kubernetes" in sources  # not drowned out by the spam source
    assert sum(1 for c in ranked if c.source == "spam") <= 3


def test_cvss_normalized_as_severity():
    c = Candidate("NVD", "CVE-2026-1", "crit", "u", cvss=9.8)
    ranked = rank_candidates([c], per_source_cap=10, per_author_cap=10)
    assert ranked[0].norm_engagement > 0.9


def test_empty_input():
    assert rank_candidates([]) == []
    assert render_priority_index([]) == "Нет ранжированных кандидатов."


def test_render_priority_index_hides_nothing_but_marks_signal():
    cands = [Candidate("HackerNews", "Hacker News", "Big news", "http://x", engagement=1234)]
    ranked = rank_candidates(cands, per_source_cap=10, per_author_cap=10)
    out = render_priority_index(ranked)
    assert "Big news" in out
    assert "http://x" in out
    assert "traction=" in out


def test_deterministic_ordering():
    # No Date.now / random in the pipeline: identical input -> identical order.
    cands = [
        Candidate("HackerNews", "Hacker News", f"t{i}", f"u{i}", engagement=500)
        for i in range(5)
    ]
    a = [c.title for c in rank_candidates(list(cands), per_source_cap=10, per_author_cap=10)]
    b = [c.title for c in rank_candidates(list(cands), per_source_cap=10, per_author_cap=10)]
    assert a == b
