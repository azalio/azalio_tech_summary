"""Unit tests for x_acquire.py — pure logic, no network/E5. Runs anywhere.

Network providers are exercised by monkeypatching x_acquire._http_get with
static feed bytes; the cascade/breaker/dedup/redaction logic is tested directly.
"""

from datetime import datetime, timedelta, timezone

import pytest

import x_acquire as xa
from x_acquire import (
    BrowserProvider,
    EmailProvider,
    NitterProvider,
    ProviderError,
    ProviderUnavailable,
    RssProvider,
    RsshubProvider,
    Source,
    TwscrapeProvider,
    XState,
    canonical_url,
    cascade_fetch,
    dedupe_in_batch,
    load_sources,
    normalize_item,
    parse_since,
    parse_sources,
    redact,
    validate_item,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>feed</title>
<item>
  <title>Kubernetes 1.40 released</title>
  <link>https://nitter.net/OpenAI/status/123#m</link>
  <description>&lt;p&gt;Big release with &lt;b&gt;new scheduler features&lt;/b&gt; and detail&lt;/p&gt;</description>
  <pubDate>Wed, 11 Jun 2025 10:00:00 GMT</pubDate>
</item>
<item>
  <title>Second post here</title>
  <link>https://nitter.net/OpenAI/status/456</link>
  <pubDate>Wed, 11 Jun 2025 11:00:00 GMT</pubDate>
</item>
</channel></rss>"""


# ── --since ──────────────────────────────────────────────────────────────────

def test_parse_since_units():
    assert parse_since("30m") == timedelta(minutes=30)
    assert parse_since("6h") == timedelta(hours=6)
    assert parse_since("2d") == timedelta(days=2)
    assert parse_since("1w") == timedelta(weeks=1)
    assert parse_since("3600") == timedelta(seconds=3600)
    assert parse_since(None) is None
    assert parse_since("") is None


def test_parse_since_invalid():
    with pytest.raises(ValueError):
        parse_since("soon")
    with pytest.raises(ValueError):
        parse_since("12x")


# ── redaction ────────────────────────────────────────────────────────────────

def test_redact_query_params():
    assert redact("https://h/twitter/user/x?key=SECRETVAL") == \
        "https://h/twitter/user/x?key=***"
    assert "auth_token=***" in redact("cookie auth_token=abcd1234; ct0=ef")
    assert "ct0=***" in redact("cookie auth_token=abcd1234; ct0=efgh5678")


def test_redact_env_secret(monkeypatch):
    monkeypatch.setenv("X_EMAIL_PASSWORD", "hunter2pass")
    assert "hunter2pass" not in redact("login failed for hunter2pass at host")
    assert "***" in redact("login failed for hunter2pass at host")


def test_redact_none_is_empty_string():
    assert redact(None) == ""


# ── canonical_url & dedup_key ────────────────────────────────────────────────

def test_canonical_url_collapses_x_mirrors():
    a = canonical_url("https://nitter.net/OpenAI/status/123#m")
    b = canonical_url("https://x.com/OpenAI/status/123")
    c = canonical_url("https://twitter.com/OpenAI/status/123?s=20")
    d = canonical_url("https://mobile.twitter.com/OpenAI/status/123")
    assert a == b == c == d == "https://x.com/OpenAI/status/123"


def test_canonical_url_generic_https():
    assert canonical_url("http://Example.com/blog/post/") == \
        "https://example.com/blog/post"


def test_dedup_key_route_independent():
    via_nitter = normalize_item(url="https://nitter.net/OpenAI/status/123#m",
                                author="@OpenAI", text="x", source="s", provider="nitter")
    via_rsshub = normalize_item(url="https://x.com/OpenAI/status/123",
                                author="@OpenAI", text="x", source="s", provider="rsshub")
    assert via_nitter["id"] == via_rsshub["id"] == "url:https://x.com/OpenAI/status/123"


def test_dedup_key_provider_id_fallback():
    it = normalize_item(url="", author="@a", text="t", source="s", provider="bluesky",
                        raw={"provider_id": "at://did:plc:abc/app.bsky.feed.post/3k"})
    assert it["id"] == "pid:bluesky:at://did:plc:abc/app.bsky.feed.post/3k"


def test_dedup_key_hash_fallback():
    it = normalize_item(url="", author="@a", text="hello world", source="s",
                        provider="email")
    assert it["id"].startswith("hash:")


# ── normalize / validate ─────────────────────────────────────────────────────

def test_normalize_item_schema_valid():
    it = normalize_item(url="https://x.com/a/status/1", author="@a", text="hi",
                        source="s", provider="rss")
    assert validate_item(it) == []
    for f in xa.REQUIRED_FIELDS:
        assert f in it


def test_validate_item_flags_problems():
    assert "empty provider" in validate_item(
        {"id": "x", "url": "", "author": "", "text": "t", "published": "",
         "source": "s", "provider": "", "fetched_at": "now", "raw": {}})
    assert any("missing field" in p for p in validate_item({"provider": "rss"}))


# ── sources ──────────────────────────────────────────────────────────────────

def test_parse_sources_fields_and_mirrors():
    data = {"sources": [
        {"id": "a", "kind": "x_user", "handle": "@OpenAI", "priority": 10,
         "bluesky": "openai.com",
         "mirrors": [{"kind": "rss", "url": "https://o/rss"}, "https://bare/feed"]},
        {"id": "b", "kind": "rss", "url": "https://b/feed"},
        {"bad": "no id — skipped"},
    ]}
    srcs = parse_sources(data)
    assert [s.id for s in srcs] == ["a", "b"]
    assert srcs[0].handle == "OpenAI"  # @ stripped
    assert srcs[0].bluesky == "openai.com"
    assert len(srcs[0].mirrors) == 2
    assert srcs[0].mirrors[1] == {"kind": "rss", "url": "https://bare/feed"}


def test_load_sources_env_fallback(monkeypatch, tmp_path):
    # Isolate from any real x_sources.yaml sitting next to the module (the file
    # legitimately wins over X_HANDLES, so the env fallback must be tested with
    # the default-file lookup pointed at a nonexistent path).
    monkeypatch.setattr(xa, "_default_sources_path",
                        lambda: str(tmp_path / "absent.yaml"))
    srcs = load_sources(env={"X_HANDLES": "OpenAI, @garrytan ,"})
    assert {s.handle for s in srcs} == {"OpenAI", "garrytan"}
    assert all(s.kind == "x_user" for s in srcs)


def test_load_sources_file_wins_over_env_handles(tmp_path):
    # Precedence: an explicit sources file overrides the X_HANDLES fallback.
    f = tmp_path / "s.yaml"
    f.write_text("sources:\n  - id: a\n    kind: x_user\n    handle: FromFile\n")
    srcs = load_sources(path=str(f), env={"X_HANDLES": "OpenAI"})
    assert [s.handle for s in srcs] == ["FromFile"]
    assert all(s.kind == "x_user" for s in srcs)


# ── RssProvider (monkeypatched HTTP) ─────────────────────────────────────────

def test_rss_provider_parses_and_strips(monkeypatch):
    monkeypatch.setattr(xa, "_http_get", lambda *a, **k: RSS_SAMPLE)
    s = Source(id="s", kind="x_user", handle="OpenAI",
               mirrors=[{"kind": "rss", "url": "https://feed"}])
    items = RssProvider().fetch(s, since_dt=None)
    assert len(items) == 2
    # The longer of title/summary becomes the body, with HTML stripped.
    assert items[0]["text"] == "Big release with new scheduler features and detail"
    assert "<" not in items[0]["text"]
    assert items[0]["author"] == "@OpenAI"
    assert items[0]["provider"] == "rss"
    assert items[0]["id"] == "url:https://x.com/OpenAI/status/123"


def test_nitter_canonicalizes_url_to_xcom(monkeypatch):
    monkeypatch.setattr(xa, "_http_get", lambda *a, **k: RSS_SAMPLE)
    s = Source(id="s", kind="x_user", handle="OpenAI")
    items = NitterProvider(instances="https://nitter.net").fetch(s, since_dt=None)
    # Stored url is rewritten to x.com (not the mirror host) for the digest + dedup.
    assert items[0]["url"] == "https://x.com/OpenAI/status/123"
    assert items[0]["provider"] == "nitter"


def test_rss_provider_keeps_mirror_url(monkeypatch):
    # An rss mirror link is NOT a status URL — must be left intact, not x.com-ified.
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Blog article title here</title>
<link>https://www.science.org/content/article/foo</link></item></channel></rss>"""
    monkeypatch.setattr(xa, "_http_get", lambda *a, **k: feed)
    s = Source(id="s", kind="rss", url="https://www.science.org/rss")
    items = RssProvider().fetch(s, since_dt=None)
    assert items[0]["url"] == "https://www.science.org/content/article/foo"


def test_rss_provider_since_filter(monkeypatch):
    monkeypatch.setattr(xa, "_http_get", lambda *a, **k: RSS_SAMPLE)
    s = Source(id="s", kind="rss", url="https://feed")
    future_cutoff = datetime(2030, 1, 1, tzinfo=UTC)
    assert RssProvider().fetch(s, since_dt=future_cutoff) == []


def test_rss_provider_all_feeds_fail_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused to secret-host?key=ABCDEF")
    monkeypatch.setattr(xa, "_http_get", boom)
    s = Source(id="s", kind="rss", url="https://feed")
    with pytest.raises(ProviderError) as ei:
        RssProvider().fetch(s, since_dt=None)
    assert "key=***" in str(ei.value)  # error is redacted


# ── provider gating: nothing configured -> supports() is False ───────────────

def test_unconfigured_providers_do_not_support():
    s = Source(id="s", kind="x_user", handle="OpenAI")
    assert RsshubProvider(base_url="").supports(s) is False
    assert NitterProvider(instances="").supports(s) is False
    assert TwscrapeProvider(env={}).supports(s) is False
    assert BrowserProvider(env={}).supports(s) is False
    assert EmailProvider(env={}).supports(s) is False


def test_rsshub_supports_when_configured():
    s = Source(id="s", kind="x_user", handle="OpenAI")
    assert RsshubProvider(base_url="http://localhost:1200").supports(s) is True


# ── cascade ──────────────────────────────────────────────────────────────────

class FakeProvider:
    def __init__(self, name, result=None, exc=None, supports=True):
        self.name = name
        self._result = result or []
        self._exc = exc
        self._supports = supports

    def supports(self, source):
        return self._supports

    def fetch(self, source, since_dt):
        if self._exc:
            raise self._exc
        return list(self._result)


def _item(url, provider):
    return normalize_item(url=url, author="@a", text="t", source="s1", provider=provider)


def test_cascade_first_success_wins():
    s = Source(id="s1", kind="x_user", handle="a", priority=1)
    providers = [
        FakeProvider("rss", result=[]),                       # reachable, empty
        FakeProvider("bluesky", result=[_item("https://b/1", "bluesky")]),
        FakeProvider("rsshub", result=[_item("https://r/9", "rsshub")]),  # never reached
    ]
    items, errors = cascade_fetch([s], providers, state=None, now=BASE)
    assert errors == 0
    assert [i["provider"] for i in items] == ["bluesky"]


def test_cascade_unavailable_skips_without_breaker(tmp_path):
    st = XState(str(tmp_path / "x.db"))
    s = Source(id="s1", kind="x_user", handle="a")
    providers = [
        FakeProvider("rss", exc=ProviderUnavailable("not configured")),
        FakeProvider("bluesky", result=[_item("https://b/1", "bluesky")]),
    ]
    items, errors = cascade_fetch([s], providers, state=st, now=BASE)
    assert errors == 0
    assert len(items) == 1
    # ProviderUnavailable must NOT count as a failure -> breaker stays closed.
    assert st.breaker_open("rss", "s1", BASE) is False
    st.close()


def test_cascade_error_records_failure_and_falls_through(tmp_path):
    st = XState(str(tmp_path / "x.db"))
    s = Source(id="s1", kind="x_user", handle="a")
    providers = [
        FakeProvider("rsshub", exc=ProviderError("boom")),
        FakeProvider("nitter", result=[_item("https://n/1", "nitter")]),
    ]
    items, errors = cascade_fetch([s], providers, state=st, now=BASE)
    assert errors == 1
    assert [i["provider"] for i in items] == ["nitter"]
    row = st.conn.execute(
        "SELECT failure_count FROM provider_state WHERE provider='rsshub'").fetchone()
    assert row[0] == 1
    st.close()


def test_cascade_all_degraded_is_not_fatal():
    s = Source(id="s1", kind="x_user", handle="a")
    providers = [FakeProvider("rss", supports=False),
                 FakeProvider("bluesky", result=[])]
    items, errors = cascade_fetch([s], providers, state=None, now=BASE)
    assert items == []
    assert errors == 0


def test_cascade_priority_order():
    lo = Source(id="lo", kind="x_user", handle="lo", priority=1)
    hi = Source(id="hi", kind="x_user", handle="hi", priority=9)
    seen = []

    class Recorder(FakeProvider):
        def fetch(self, source, since_dt):
            seen.append(source.id)
            return [_item(f"https://x/{source.id}", "rss")]

    cascade_fetch([lo, hi], [Recorder("rss")], state=None, now=BASE)
    assert seen == ["hi", "lo"]  # higher priority first


# ── circuit breaker (XState) ─────────────────────────────────────────────────

def test_breaker_opens_after_threshold_and_resets(tmp_path):
    st = XState(str(tmp_path / "x.db"), breaker_threshold=3, cooldown_base_min=30)
    for _ in range(2):
        st.record_failure("rsshub", "s1", "err", now=BASE)
        assert st.breaker_open("rsshub", "s1", BASE) is False  # below threshold
    st.record_failure("rsshub", "s1", "err", now=BASE)         # 3rd -> opens
    assert st.breaker_open("rsshub", "s1", BASE) is True
    assert st.breaker_open("rsshub", "s1", BASE + timedelta(minutes=10)) is True
    assert st.breaker_open("rsshub", "s1", BASE + timedelta(minutes=31)) is False
    # A success clears the breaker entirely.
    st.record_success("rsshub", "s1")
    assert st.breaker_open("rsshub", "s1", BASE) is False
    st.close()


def test_breaker_backoff_grows(tmp_path):
    st = XState(str(tmp_path / "x.db"), breaker_threshold=1,
                cooldown_base_min=30, cooldown_max_min=360)
    st.record_failure("p", "s", "e", now=BASE)   # extra=0 -> 30m
    assert st.breaker_open("p", "s", BASE + timedelta(minutes=29)) is True
    assert st.breaker_open("p", "s", BASE + timedelta(minutes=31)) is False
    st.record_failure("p", "s", "e", now=BASE)   # extra=1 -> 60m
    assert st.breaker_open("p", "s", BASE + timedelta(minutes=59)) is True
    st.close()


def test_failure_error_is_redacted_in_state(tmp_path, monkeypatch):
    monkeypatch.setenv("X_RSSHUB_ACCESS_KEY", "TOPSECRETKEY")
    st = XState(str(tmp_path / "x.db"))
    st.record_failure("rsshub", "s1", "failed with TOPSECRETKEY in url", now=BASE)
    row = st.conn.execute("SELECT last_error FROM provider_state").fetchone()
    assert "TOPSECRETKEY" not in row[0]
    assert "***" in row[0]
    st.close()


# ── seen-item dedup ──────────────────────────────────────────────────────────

def test_filter_new_and_mark_seen(tmp_path):
    st = XState(str(tmp_path / "x.db"))
    items = [_item("https://x.com/a/status/1", "rss"),
             _item("https://x.com/a/status/2", "rss")]
    assert len(st.filter_new(items)) == 2
    st.mark_seen(items[:1])
    new = st.filter_new(items)
    assert [i["url"] for i in new] == ["https://x.com/a/status/2"]
    st.close()


# ── batch dedup ──────────────────────────────────────────────────────────────

def test_dedupe_in_batch_collapses_same_post():
    a = _item("https://nitter.net/OpenAI/status/123#m", "nitter")
    b = _item("https://x.com/OpenAI/status/123", "rsshub")
    c = _item("https://x.com/OpenAI/status/999", "rsshub")
    out = dedupe_in_batch([a, b, c])
    assert len(out) == 2
    assert out[0]["provider"] == "nitter"  # first occurrence kept
