"""E2E tests for vibe-intel deduplication system.

Runs with real E5 model on the server. Each test uses its own temp DB dir.
"""

import shutil
import sqlite3
import time
import tempfile

import pytest

from dedup import (
    EventDedup,
    normalize_headline,
    extract_tokens,
    canonicalize,
    jaccard_similarity,
)
from collectors import Collectors
from core import VibeCore


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temp dir for EventDedup DB, cleaned up after test."""
    return str(tmp_path / "dedup_db")


@pytest.fixture
def dedup(tmp_db):
    """EventDedup instance with default params and temp DB."""
    d = EventDedup(
        db_dir=tmp_db,
        match_threshold=0.80,
        ttl_hours=168,
        matching_ttl_hours=48,
        max_cluster_size=50,
        dry_run=False,
    )
    yield d
    d.close()


@pytest.fixture
def tmp_sent_db(tmp_path):
    """Temp SQLite DB for URL dedup (sent_posts)."""
    db_path = str(tmp_path / "sent.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_posts (
            url TEXT PRIMARY KEY,
            subreddit TEXT,
            sent_at TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    return db_path


# ═══════════════════════════════════════════════════════════════════════════
# 1. Unit tests for utilities
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalizeHeadline:
    def test_strip_source_suffix(self):
        assert "AI breaks record" in normalize_headline("AI breaks record - BBC")
        assert "BBC" not in normalize_headline("AI breaks record - BBC")

    def test_strip_leading_tag(self):
        assert normalize_headline("BREAKING: AI breaks record") == "AI breaks record"
        assert normalize_headline("EXCLUSIVE: New chip - CNN") == "New chip"

    def test_strip_trailing_noise(self):
        assert normalize_headline("AI model released, report says") == "AI model released"

    def test_unicode_normalization(self):
        result = normalize_headline("\u201cHello\u201d \u2014 World")
        assert '"' in result
        assert "-" in result

    def test_whitespace_collapse(self):
        assert "  " not in normalize_headline("too   many   spaces")


class TestExtractTokens:
    def test_filters_stopwords_en(self):
        tokens = extract_tokens("the quick fox and the lazy dog")
        assert "the" not in tokens
        assert "and" not in tokens
        assert "quick" in tokens
        assert "fox" in tokens

    def test_filters_stopwords_ru(self):
        tokens = extract_tokens("это был тест для проверки системы")
        assert "это" not in tokens
        assert "был" not in tokens
        assert "тест" in tokens

    def test_min_length_3(self):
        tokens = extract_tokens("AI is ok but GPT works")
        assert "is" not in tokens
        assert "ok" not in tokens
        assert "GPT" not in tokens  # lowercase "gpt" — 3 chars, in set
        assert "ai" in tokens
        # "works" should be present
        assert "works" in tokens

    def test_strips_english_possessive(self):
        tokens = extract_tokens("Uber's CTO discusses AI spending")
        assert "uber" in tokens
        assert "uber's" not in tokens

    def test_mixed_languages(self):
        tokens = extract_tokens("Kubernetes кластер production ready")
        assert "kubernetes" in tokens
        assert "кластер" in tokens
        assert "production" in tokens


class TestCanonicalize:
    def test_entity_aliases(self):
        tokens = {"франция", "trump", "chatgpt"}
        canon = canonicalize(tokens)
        assert "france" in canon
        assert "trump" in canon
        assert "openai" in canon

    def test_unknown_tokens_unchanged(self):
        tokens = {"kubernetes", "docker"}
        canon = canonicalize(tokens)
        assert canon == {"kubernetes", "docker"}


class TestJaccardSimilarity:
    def test_identical(self):
        assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0

    def test_disjoint(self):
        assert jaccard_similarity({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        j = jaccard_similarity({"a", "b", "c"}, {"b", "c", "d"})
        assert abs(j - 0.5) < 0.01  # 2/4

    def test_empty_sets(self):
        assert jaccard_similarity(set(), set()) == 0.0
        assert jaccard_similarity({"a"}, set()) == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 2. EventDedup tests with real model
# ═══════════════════════════════════════════════════════════════════════════

class TestEventDedupBasic:
    def test_new_item_returns_true(self, dedup):
        assert dedup.check_and_add("SpaceX launches Falcon 9", "test", "http://a.com") is True

    def test_exact_duplicate_returns_false(self, dedup):
        dedup.check_and_add("SpaceX launches Falcon 9", "src1", "http://a.com")
        assert dedup.check_and_add("SpaceX launches Falcon 9", "src2", "http://b.com") is False

    def test_stats_counting(self, dedup):
        dedup.check_and_add("Kubernetes 1.32 introduces gateway API improvements", "a", "http://1.com")
        dedup.check_and_add("Python 3.14 adds native JIT compilation support", "b", "http://2.com")
        dedup.check_and_add("Kubernetes 1.32 introduces gateway API improvements", "c", "http://3.com")
        stats = dedup.stats()
        assert stats["checked"] == 3
        assert stats["duplicates"] == 1
        assert stats["added"] == 2

    def test_event_signals_report_multi_source_burst(self, dedup):
        dedup.check_and_add("Anthropic releases Claude Opus 4.8", "Telegram:@testingcatalog", "http://a.com")
        dedup.check_and_add("Anthropic releases Claude Opus 4.8", "ClaudePlatform", "http://b.com")
        dedup.check_and_add("Anthropic releases Claude Opus 4.8", "Telegram:@dofh_ru", "http://c.com")

        signals = dedup.event_signals()

        assert len(signals) == 1
        assert signals[0]["title"] == "Anthropic releases Claude Opus 4.8"
        assert signals[0]["observations"] == 3
        assert signals[0]["source_count"] == 3
        assert signals[0]["source_burst"] == "high"
        assert signals[0]["sources"] == [
            "ClaudePlatform",
            "Telegram:@dofh_ru",
            "Telegram:@testingcatalog",
        ]


class TestTieredGate:
    def test_high_similarity_no_jaccard_needed(self, dedup):
        """Nearly identical titles with minor wording changes → duplicate."""
        dedup.check_and_add("OpenAI releases GPT-5 model to the public", "src1", "http://a.com")
        # Same event, slightly different wording
        result = dedup.check_and_add("OpenAI launches GPT-5 model to the public", "src2", "http://b.com")
        assert result is False

    def test_medium_similarity_with_jaccard(self, dedup):
        """Same event, different wording but shared keywords → duplicate."""
        dedup.check_and_add(
            "Kubernetes 1.32 released with gateway API improvements",
            "src1", "http://a.com",
        )
        result = dedup.check_and_add(
            "Kubernetes 1.32 release brings gateway API and security fixes",
            "src2", "http://b.com",
        )
        assert result is False

    def test_low_similarity_passes(self, dedup):
        """Completely different topics → both pass."""
        dedup.check_and_add("NASA discovers high-energy gamma rays from black hole", "src1", "http://a.com")
        result = dedup.check_and_add("Python 3.14 introduces native JIT compiler", "src2", "http://b.com")
        assert result is True

    def test_same_domain_different_events(self, dedup):
        """Same domain but genuinely different events → both pass."""
        dedup.check_and_add(
            "Google announces Gemini 3.0 with multimodal capabilities",
            "src1", "http://a.com",
        )
        result = dedup.check_and_add(
            "Google Cloud reports major outage affecting US-East region",
            "src2", "http://b.com",
        )
        assert result is True


class TestMatchingTTL:
    def test_matching_ttl_skips_old_clusters(self, tmp_db):
        """Cluster older than matching_ttl should not block new items."""
        dedup = EventDedup(db_dir=tmp_db, matching_ttl_hours=2, max_cluster_size=50)
        dedup.check_and_add("CISA adds critical vulnerability to catalog", "src1", "http://a.com")

        # Patch first_seen to 3 hours ago
        three_hours_ago = time.time() - 3 * 3600
        dedup._conn.execute(
            "UPDATE event_clusters SET first_seen = ?", (three_hours_ago,)
        )
        dedup._conn.commit()
        # Reload clusters
        dedup._clusters = dedup._load_clusters()

        # Same title should now pass (cluster is stale)
        result = dedup.check_and_add("CISA adds critical vulnerability to catalog", "src2", "http://b.com")
        assert result is True
        dedup.close()


class TestMaxClusterSize:
    def test_max_cluster_size_skips_large_clusters(self, tmp_db):
        """Cluster at max size should not block new items."""
        dedup = EventDedup(db_dir=tmp_db, max_cluster_size=5)
        dedup.check_and_add("CISA adds vulnerability to KEV catalog", "src1", "http://a.com")

        # Patch item_count to max
        dedup._conn.execute("UPDATE event_clusters SET item_count = 5")
        dedup._conn.commit()
        dedup._clusters = dedup._load_clusters()

        result = dedup.check_and_add("CISA adds vulnerability to KEV catalog", "src2", "http://b.com")
        assert result is True
        dedup.close()


class TestTitleOnlyJaccard:
    def test_jaccard_uses_title_only(self, dedup):
        """Long description should NOT dilute Jaccard — regression test."""
        title = "ОАЭ выходят из ОПЕК"
        long_desc_1 = (
            "Объединенные Арабские Эмираты объявили о выходе из Организации "
            "стран-экспортеров нефти с первого мая текущего года. "
            "Решение крупного производителя нефти может дестабилизировать рынок."
        )
        long_desc_2 = (
            "Министр энергетики ОАЭ заявил что решение о выходе из ОПЕК "
            "является суверенным и направлено на диверсификацию экономики. "
            "Реакция рынков была негативной, цены на нефть выросли."
        )
        dedup.check_and_add(title, "src1", "http://a.com", long_desc_1)
        result = dedup.check_and_add(title, "src2", "http://b.com", long_desc_2)
        assert result is False, "Same title with different descriptions should be deduplicated"

    def test_different_titles_different_descs_pass(self, dedup):
        """Different titles about different events should pass even with similar descriptions."""
        dedup.check_and_add(
            "Apple announces iPhone 17 with new chip",
            "src1", "http://a.com",
            "Apple unveiled its latest smartphone at a Cupertino event.",
        )
        result = dedup.check_and_add(
            "Samsung Galaxy S27 features Snapdragon 9 Gen 5",
            "src2", "http://b.com",
            "Samsung unveiled its latest smartphone at an Unpacked event.",
        )
        assert result is True


class TestCrossLanguageDedup:
    def test_cross_language_same_event(self, dedup):
        """Multilingual model should detect same event in EN and RU."""
        dedup.check_and_add(
            "UAE announces exit from OPEC starting May 2026",
            "reuters", "http://a.com",
        )
        result = dedup.check_and_add(
            "ОАЭ объявили о выходе из ОПЕК с мая 2026 года",
            "interfax", "http://b.com",
        )
        # With multilingual E5 model, embedding similarity should be >= 0.90
        assert result is False, "Same event in different languages should be deduplicated"

    def test_uber_ai_budget_chinese_to_reddit(self, dedup):
        """Regression: short AI token must survive token overlap gate."""
        dedup.check_and_add(
            "四个月花光全年 AI 预算，Uber 总裁质疑 AI 投入合理性",
            "CHINA TECH:ITHome",
            "https://www.ithome.com/0/955/563.htm",
        )

        result = dedup.check_and_add(
            "So, Uber CTO said that Uber burned their total 2026 AI budget within the first four months",
            "Reddit:r/ChatGPT",
            "https://reddit.com/r/ChatGPT/comments/1tp7ips/so_uber_cto_said_that_uber_burned_their_total/",
        )

        assert result is False, "Same Uber AI budget story should be deduplicated"


class TestCleanup:
    def test_cleanup_removes_old_clusters(self, tmp_db):
        dedup = EventDedup(db_dir=tmp_db, ttl_hours=1)
        dedup.check_and_add("Old story about Mars", "src", "http://a.com")

        # Patch timestamps to 2 hours ago
        two_hours_ago = time.time() - 2 * 3600
        dedup._conn.execute(
            "UPDATE event_clusters SET last_seen = ?, first_seen = ?",
            (two_hours_ago, two_hours_ago),
        )
        dedup._conn.commit()

        # Run cleanup
        dedup._cleanup()
        count = dedup._conn.execute("SELECT COUNT(*) FROM event_clusters").fetchone()[0]
        assert count == 0
        dedup.close()

    def test_cleanup_removes_old_items(self, tmp_db):
        dedup = EventDedup(db_dir=tmp_db, ttl_hours=1)
        dedup.check_and_add("Old item story", "src", "http://a.com")

        two_hours_ago = time.time() - 2 * 3600
        dedup._conn.execute(
            "UPDATE cluster_items SET created_at = ?", (two_hours_ago,)
        )
        dedup._conn.execute(
            "UPDATE event_clusters SET last_seen = ?, first_seen = ?",
            (two_hours_ago, two_hours_ago),
        )
        dedup._conn.commit()

        dedup._cleanup()
        items = dedup._conn.execute("SELECT COUNT(*) FROM cluster_items").fetchone()[0]
        assert items == 0
        dedup.close()


class TestDryRun:
    def test_dry_run_logs_but_passes(self, tmp_db):
        dedup = EventDedup(db_dir=tmp_db, dry_run=True)
        dedup.check_and_add("Same news repeated", "src1", "http://a.com")
        result = dedup.check_and_add("Same news repeated", "src2", "http://b.com")
        assert result is True, "Dry run should return True even for duplicates"
        assert dedup.stats()["duplicates"] == 1
        dedup.close()


# ═══════════════════════════════════════════════════════════════════════════
# 3. URL dedup tests (Collectors)
# ═══════════════════════════════════════════════════════════════════════════

class TestNormalizeUrl:
    def test_https_upgrade(self):
        assert Collectors._normalize_url("http://example.com/path") == "https://example.com/path"

    def test_lowercase_host(self):
        assert Collectors._normalize_url("https://EXAMPLE.COM/Path") == "https://example.com/Path"

    def test_strip_trailing_slash(self):
        assert Collectors._normalize_url("https://example.com/path/") == "https://example.com/path"

    def test_strip_utm_params(self):
        url = "https://example.com/article?utm_source=twitter&utm_medium=social&id=123"
        result = Collectors._normalize_url(url)
        assert "utm_source" not in result
        assert "id=123" in result

    def test_sort_params(self):
        url1 = Collectors._normalize_url("https://example.com?b=2&a=1")
        url2 = Collectors._normalize_url("https://example.com?a=1&b=2")
        assert url1 == url2


def _bare_collectors(db_path):
    """Build a Collectors with __init__ bypassed but enough wired up for
    URL-dedup tests. Used by tests that don't want the full workspace setup."""
    c = Collectors.__new__(Collectors)
    c.db_path = db_path
    c.dedup = None
    c._pending_marks = []
    c._pending_marks_set = set()
    return c


class TestUrlDedup:
    def test_is_seen_mark_seen_roundtrip(self, tmp_sent_db):
        c = _bare_collectors(tmp_sent_db)

        assert c._is_seen("https://example.com/article") is False
        c._mark_seen("https://example.com/article", "test")
        # In-run: queued URLs are "seen" via the pending set.
        assert c._is_seen("https://example.com/article") is True

    def test_url_normalization_in_dedup(self, tmp_sent_db):
        """Mark with tracking params, check without → should match."""
        c = _bare_collectors(tmp_sent_db)

        c._mark_seen("https://example.com/article?utm_source=twitter", "test")
        assert c._is_seen("https://example.com/article") is True

    def test_empty_url_not_seen(self, tmp_sent_db):
        c = _bare_collectors(tmp_sent_db)

        assert c._is_seen("") is False
        assert c._is_seen(None) is False


class TestDeferredCommit:
    """Regression tests for GitHub issue #2: URL marks must not persist to
    sent_posts until commit_seen() runs, so a crash between collection and
    Telegram delivery can't permanently drop items."""

    def test_mark_seen_does_not_persist_until_commit(self, tmp_sent_db):
        c = _bare_collectors(tmp_sent_db)
        c._mark_seen("https://example.com/a", "test")
        c._mark_seen("https://example.com/b", "test")

        # Pending queue holds them but sent_posts is still empty.
        assert len(c._pending_marks) == 2
        conn = sqlite3.connect(tmp_sent_db)
        rows = conn.execute("SELECT COUNT(*) FROM sent_posts").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_commit_seen_persists_and_clears(self, tmp_sent_db):
        c = _bare_collectors(tmp_sent_db)
        c._mark_seen("https://example.com/a", "test")
        c._mark_seen("https://example.com/b", "test")
        c.commit_seen()

        conn = sqlite3.connect(tmp_sent_db)
        urls = {row[0] for row in conn.execute("SELECT url FROM sent_posts")}
        conn.close()
        assert urls == {"https://example.com/a", "https://example.com/b"}
        # Idempotent: second commit is a no-op.
        assert c._pending_marks == []
        assert c._pending_marks_set == set()
        c.commit_seen()  # must not raise

    def test_uncommitted_marks_dont_leak_to_fresh_collectors(self, tmp_sent_db):
        """If send_tg fails, a fresh Collectors next run must see the URLs as
        not-seen — this is the whole point of deferred marking."""
        c1 = _bare_collectors(tmp_sent_db)
        c1._mark_seen("https://example.com/lost", "test")
        # Simulate process death before commit_seen.

        c2 = _bare_collectors(tmp_sent_db)
        assert c2._is_seen("https://example.com/lost") is False

    def test_committed_marks_survive_into_fresh_collectors(self, tmp_sent_db):
        c1 = _bare_collectors(tmp_sent_db)
        c1._mark_seen("https://example.com/kept", "test")
        c1.commit_seen()

        c2 = _bare_collectors(tmp_sent_db)
        assert c2._is_seen("https://example.com/kept") is True

    def test_mark_seen_is_idempotent_within_run(self, tmp_sent_db):
        c = _bare_collectors(tmp_sent_db)
        c._mark_seen("https://example.com/x", "src1")
        c._mark_seen("https://example.com/x", "src2")  # duplicate
        assert len(c._pending_marks) == 1
        # First source wins (matches old INSERT OR IGNORE semantics).
        assert c._pending_marks[0][1] == "src1"

    def test_commit_seen_failure_preserves_queue(self, tmp_path):
        """If SQLite write fails, the in-memory queue must survive so a
        future retry (or the next call) can re-attempt the writes."""
        # Parent directory doesn't exist → sqlite3.connect raises
        # OperationalError, exercising the except path in commit_seen.
        c = _bare_collectors(str(tmp_path / "missing-subdir" / "sent.db"))
        c._mark_seen("https://example.com/a", "test")
        c._mark_seen("https://example.com/b", "test")

        c.commit_seen()  # logs error, must not raise

        assert len(c._pending_marks) == 2
        assert c._pending_marks_set == {
            "https://example.com/a",
            "https://example.com/b",
        }


# ═══════════════════════════════════════════════════════════════════════════
# 4. E2E scenario tests
# ═══════════════════════════════════════════════════════════════════════════

class TestHourlyRunScenario:
    def test_three_hourly_runs(self, dedup):
        """Simulate 3 hourly collection runs."""
        unique_titles = [
            "AWS announces new region in Tokyo expansion",
            "Critical CVE-2026-1234 found in OpenSSL",
            "Terraform 2.0 released with native Kubernetes support",
            "GitHub Copilot adds real-time code review feature",
            "Cloudflare reports DDoS attack reaching 5 Tbps",
        ]

        # Hour 1: all unique
        for i, title in enumerate(unique_titles):
            assert dedup.check_and_add(title, f"src{i}", f"http://{i}.com") is True

        # Hour 2: same titles should be blocked
        for i, title in enumerate(unique_titles):
            assert dedup.check_and_add(title, f"src{i}b", f"http://{i}b.com") is False

        # Hour 2: new titles should pass
        new_titles = [
            "Linux kernel 7.0 released with RISC-V improvements",
            "Docker acquires container security startup",
        ]
        for i, title in enumerate(new_titles):
            assert dedup.check_and_add(title, f"new{i}", f"http://new{i}.com") is True

        stats = dedup.stats()
        assert stats["checked"] == 12
        assert stats["duplicates"] == 5
        assert stats["added"] == 7


class TestOpecScenario:
    def test_opec_cross_source(self, dedup):
        """UAE leaves OPEC — same event from multiple sources."""
        sources = [
            ("UAE leaves OPEC in blow to global oil producers' group", "finnhub:Reuters"),
            ("UAE to Exit OPEC After Nearly 60 Years", "TECH NEWS:Wired"),
            ("ОАЭ объявили о выходе из ОПЕК с мая 2026", "ru_news:Interfax"),
            ("UAE Leaves OPEC and OPEC+", "HackerNews"),
        ]
        results = []
        for i, (title, source) in enumerate(sources):
            r = dedup.check_and_add(title, source, f"http://opec{i}.com")
            results.append(r)

        # First should pass, rest should be deduplicated
        assert results[0] is True
        passed = sum(1 for r in results if r is True)
        assert passed <= 2, f"Expected at most 2 to pass, got {passed}"


class TestRecapFilter:
    def test_recap_titles_dropped(self, tmp_path):
        """Week-in-review / weekly roundup articles must be dropped upfront."""
        c = Collectors.__new__(Collectors)
        c.workspace = str(tmp_path)
        c.db_path = str(tmp_path / "sent.db")
        c.dedup = None

        recaps = [
            "Week in review: High-severity LPE vulnerability in the Linux kernel, cPanel 0-day exploited for months",
            "Weekly Roundup: Top AI papers of the week",
            "Weekly Recap: What happened in cloud this week",
            "This Week in Kubernetes: 1.32 release",
            "In Review: AWS outages and DDoS attacks",
        ]
        for title in recaps:
            assert c._is_semantic_dup(title, "src", "http://x.com") is True, f"recap not dropped: {title!r}"

    def test_normal_titles_pass_recap_filter(self, tmp_path):
        """Regular news titles must NOT be flagged as recaps."""
        c = Collectors.__new__(Collectors)
        c.workspace = str(tmp_path)
        c.db_path = str(tmp_path / "sent.db")
        c.dedup = None  # disables semantic check, only recap filter applies

        regular = [
            "OpenAI releases GPT-5 model to the public",
            "Critical CVE-2026-1234 found in OpenSSL",
            "Linux kernel 7.0 ships with RISC-V improvements",
            "AWS announces new region in Tokyo",
        ]
        for title in regular:
            assert c._is_semantic_dup(title, "src", "http://x.com") is False, f"false positive: {title!r}"


class TestEventSignalPrompt:
    def test_format_event_signals_for_prompt(self):
        from main import format_event_signals

        text = format_event_signals([
            {
                "cluster_id": 42,
                "title": "Anthropic releases Claude Opus 4.8",
                "observations": 5,
                "source_count": 3,
                "source_burst": "high",
                "sources": ["ClaudePlatform", "Telegram:@testingcatalog"],
            }
        ])

        assert "event_id=42" in text
        assert "source_burst=high" in text
        assert "observations=5" in text
        assert "source_count=3" in text
        assert "ranking_signal_only" in text
        assert "Anthropic releases Claude Opus 4.8" in text


class TestRussianStylePrompt:
    def test_prompt_requires_russian_technical_prose(self):
        from main import VIBE_PROMPT

        assert "РУССКИЙ ТЕХНИЧЕСКИЙ СТИЛЬ" in VIBE_PROMPT
        assert "необоснованные английские словосочетания" in VIBE_PROMPT
        assert "clean-room open implementation" in VIBE_PROMPT
        assert "независимая открытая реализация" in VIBE_PROMPT
        assert "agent workloads" in VIBE_PROMPT
        assert "нагрузки AI-агентов" in VIBE_PROMPT
        assert "оставляй в оригинале только" in VIBE_PROMPT


class TestCisaScenario:
    def test_cisa_cluster_cap(self):
        """CISA alerts should not all collapse into one cluster."""
        tmp = tempfile.mkdtemp()
        dedup = EventDedup(db_dir=tmp, max_cluster_size=3)

        titles = [
            "CISA Adds One Known Exploited Vulnerability to Catalog",
            "CISA Adds One Known Exploited Vulnerability to Catalog",
            "CISA Adds One Known Exploited Vulnerability to Catalog",
            "CISA Adds One Known Exploited Vulnerability to Catalog",
            "CISA Adds One Known Exploited Vulnerability to Catalog",
        ]
        results = []
        for i, title in enumerate(titles):
            r = dedup.check_and_add(title, "CISA", f"http://cisa{i}.gov")
            results.append(r)

        # First passes, next 2 blocked (cluster fills to 3), then cluster capped →
        # 4th creates new cluster (passes), 5th and 6th blocked again
        assert results[0] is True
        # At least 2 should pass (first cluster + overflow to new cluster)
        passed = sum(1 for r in results if r is True)
        assert passed >= 2, f"Expected >=2 to pass with max_cluster_size=3, got {passed}"

        dedup.close()
        shutil.rmtree(tmp)


# ═══════════════════════════════════════════════════════════════════════════
# 5. send_tg long-section splitting (core.VibeCore)
# ═══════════════════════════════════════════════════════════════════════════

def _bare_core():
    """VibeCore with __init__ bypassed — its constructor demands a real TG
    token but the helpers under test only touch local strings."""
    c = VibeCore.__new__(VibeCore)
    c.TG_LIMIT = 4000
    return c


class TestSplitOversizedSection:
    def test_short_section_returned_as_is(self):
        c = _bare_core()
        section = "<b>🔥 Title</b>\n• one\n• two"
        assert c._split_oversized_section(section, max_len=4000) == [section]

    def test_long_section_split_by_lines(self):
        c = _bare_core()
        header = "<b>🤖 AI / ML</b>"
        bullets = [f"• bullet {i} " + "x" * 200 for i in range(30)]
        section = "\n".join([header] + bullets)
        max_len = 1000

        chunks = c._split_oversized_section(section, max_len=max_len)

        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= max_len, f"chunk too long: {len(chunk)}"
            # Every chunk must carry the header so context isn't lost
            assert chunk.startswith(header)
            # No chunk should be header-only
            assert "\n" in chunk

    def test_section_without_header_still_splits(self):
        c = _bare_core()
        section = "\n".join([f"• item {i} " + "y" * 150 for i in range(20)])
        chunks = c._split_oversized_section(section, max_len=500)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 500
            assert "<b>" not in chunk  # no header invented out of thin air

    def test_oversized_single_line_passes_through(self):
        """If a single line itself exceeds max_len we emit it anyway — better
        to let plain-text fallback strip HTML than truncate mid-tag."""
        c = _bare_core()
        section = "<b>X</b>\n• " + "z" * 5000
        chunks = c._split_oversized_section(section, max_len=1000)
        assert len(chunks) == 1
        # The long line is preserved (no truncation)
        assert "z" * 5000 in chunks[0]
