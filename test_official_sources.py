"""Official source collectors: mocked HTTP, no DB/model/network."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import feedparser
import pytest
import requests

from collectors import Collectors


@pytest.fixture
def collector():
    c = object.__new__(Collectors)
    c.candidates = []
    c.source_counts = {}
    c._is_seen = Mock(return_value=False)
    c._mark_seen = Mock()
    c._is_semantic_dup = Mock(return_value=False)
    return c


def entry(tag="v2.1.296", repo="anthropics/claude-code", age=1, **changes):
    e = {
        "title": tag,
        "link": f"https://github.com/{repo}/releases/tag/{tag}",
        "updated": (datetime.now(timezone.utc) - timedelta(days=age)).isoformat(),
        "summary": "<h2>Changes</h2><p>New hooks &amp; reliable retries.</p>",
    }
    e.update(changes)
    return e


def serve(monkeypatch, pages):
    responses = []
    parsed = {}
    for i, page in enumerate(pages):
        if isinstance(page, Exception):
            responses.append(page)
        else:
            content = str(i).encode()
            parsed[content] = feedparser.FeedParserDict(entries=page)
            responses.append(Mock(content=content))
    get = Mock(side_effect=responses)
    monkeypatch.setattr("collectors.requests.get", get)
    monkeypatch.setattr("collectors.feedparser.parse", lambda content: parsed[content])
    return get


def test_stable_releases_and_candidate_parity(collector, monkeypatch):
    get = serve(monkeypatch, [
        [entry()],
        [entry("rust-v0.162.1", "openai/codex")],
        [entry("v0.5.21", "sgl-project/sglang")],
    ])
    out = collector.collect_official_releases()
    assert "Claude Code v2.1.296" in out
    assert "Codex CLI rust-v0.162.1" in out
    assert "SGLang v0.5.21" in out
    assert "New hooks & reliable retries." in out
    assert "<p>" not in out
    assert len(collector.candidates) == 3
    assert collector.source_counts == {"OFFICIAL RELEASES": 3}
    assert all(c.engagement is None for c in collector.candidates)
    assert all(call.kwargs["timeout"] == 15 for call in get.call_args_list)
    assert all("api.github.com" not in call.args[0] for call in get.call_args_list)


@pytest.mark.parametrize("tag", ["v2.1.0-alpha.1", "v2.1.0rc1", "v2.1.0-beta", "nightly", "sdk/v1.2.3", "v1.2"])
def test_nonstable_never_touches_state(collector, monkeypatch, tag):
    serve(monkeypatch, [[entry(tag, title="v2.1.296")], [], []])
    assert collector.collect_official_releases() == ""
    collector._mark_seen.assert_not_called()
    collector._is_semantic_dup.assert_not_called()


@pytest.mark.parametrize("link", [
    "https://example.com/anthropics/claude-code/releases/tag/v2.1.296",
    "https://github.com/other/repo/releases/tag/v2.1.296",
    "http://github.com/anthropics/claude-code/releases/tag/v2.1.296",
    "https://github.com/anthropics/claude-code/releases/tag/v2.1.296?bad=1",
    "https://[invalid", None,
])
def test_invalid_release_links(collector, monkeypatch, link):
    serve(monkeypatch, [[entry(link=link)], [], []])
    assert collector.collect_official_releases() == ""
    collector._mark_seen.assert_not_called()


@pytest.mark.parametrize("change", [{"age": 20}, {"updated": None}, {"updated": "bad"}, {"summary": ""}, {"summary": None}])
def test_missing_or_old_records(collector, monkeypatch, change):
    serve(monkeypatch, [[entry(**change)], [], []])
    assert collector.collect_official_releases() == ""
    collector._mark_seen.assert_not_called()


def test_newest_first_and_two_per_product(collector, monkeypatch):
    serve(monkeypatch, [[entry("v2.1.290", age=20), entry("v2.1.291", age=3),
                           entry("v2.1.292", age=2), entry("v2.1.293", age=1)], [], []])
    out = collector.collect_official_releases()
    assert "v2.1.293" in out and "v2.1.292" in out
    assert "v2.1.291" not in out and "v2.1.290" not in out
    assert out.index("v2.1.293") < out.index("v2.1.292")
    assert len(collector.candidates) == 2


def test_seen_and_semantic_duplicates(collector, monkeypatch):
    collector._is_seen.side_effect = [True, False, False]
    collector._is_semantic_dup.side_effect = [True, False]
    serve(monkeypatch, [[entry("v2.1.293"), entry("v2.1.292", age=2), entry("v2.1.291", age=3)], [], []])
    out = collector.collect_official_releases()
    assert "v2.1.291" in out and "v2.1.293" not in out and "v2.1.292" not in out
    assert len(collector.candidates) == 1


def test_long_notes_and_scripts(collector, monkeypatch):
    serve(monkeypatch, [[entry(summary="<script>bad()</script><style>css</style><p>" + "x" * 8000 + "</p>")], [], []])
    out = collector.collect_official_releases()
    assert "bad()" not in out and "css" not in out
    assert "x" * 6000 in out and "x" * 6001 not in out
    assert "truncated" in out
    assert len(collector._is_semantic_dup.call_args.args[3]) == 300


@pytest.mark.parametrize("error", [requests.Timeout("timeout"), requests.HTTPError("503")])
def test_partial_failure(collector, monkeypatch, error):
    serve(monkeypatch, [error, [], [entry("v0.5.21", "sgl-project/sglang")]])
    assert "SGLang" in collector.collect_official_releases()
    assert len(collector.candidates) == 1


def test_malformed_xml_is_logged_and_other_feeds_survive(collector, monkeypatch, capsys):
    get = Mock(return_value=Mock(content=b"bad"))
    monkeypatch.setattr("collectors.requests.get", get)
    monkeypatch.setattr("collectors.feedparser.parse", Mock(side_effect=[
        feedparser.FeedParserDict(bozo=True, entries=[]),
        feedparser.FeedParserDict(entries=[]),
        feedparser.FeedParserDict(entries=[entry("v0.5.21", "sgl-project/sglang")]),
    ]))
    assert "SGLang" in collector.collect_official_releases()
    assert "error" in capsys.readouterr().out.lower()


def test_content_fallback(collector, monkeypatch):
    serve(monkeypatch, [[entry(summary=None, content=[{"value": "<p>New capability</p>"}])], [], []])
    assert "New capability" in collector.collect_official_releases()


def test_malformed_entry_does_not_hide_valid_release(collector, monkeypatch):
    serve(monkeypatch, [[None, entry(link=[]), entry(summary=[]), entry()], [], []])
    assert "Claude Code v2.1.296" in collector.collect_official_releases()
    assert len(collector.candidates) == 1


def test_fourteen_day_window_and_freshness(collector, monkeypatch):
    serve(monkeypatch, [[], [], [entry("v0.5.21", "sgl-project/sglang", age=8),
                                   entry("v0.5.20", "sgl-project/sglang", age=15),
                                   entry("v0.5.22", "sgl-project/sglang", age=-1)]])
    out = collector.collect_official_releases()
    assert "v0.5.21" in out and "v0.5.20" not in out and "v0.5.22" not in out
    assert collector.candidates[0].freshness == pytest.approx(6 / 14, abs=0.001)


def test_six_total_and_atom_endpoints(collector, monkeypatch):
    repos = ["anthropics/claude-code", "openai/codex", "sgl-project/sglang"]
    get = serve(monkeypatch, [[entry(("rust-v" if repo == "openai/codex" else "v") + f"1.2.{n}", repo, age=n)
                              for n in range(1, 4)] for repo in repos])
    collector.collect_official_releases()
    assert len(collector.candidates) == 6
    assert [call.args[0] for call in get.call_args_list] == [
        f"https://github.com/{repo}/releases.atom" for repo in repos
    ]


def test_parser_exception_isolated(collector, monkeypatch):
    monkeypatch.setattr("collectors.requests.get", Mock(return_value=Mock(content=b"xml")))
    monkeypatch.setattr("collectors.feedparser.parse", Mock(side_effect=[
        ValueError("bad XML"), feedparser.FeedParserDict(entries=[]),
        feedparser.FeedParserDict(entries=[entry("v0.5.21", "sgl-project/sglang")]),
    ]))
    assert "SGLang" in collector.collect_official_releases()


def test_tailscale_feed_and_caps(collector):
    collector._fetch_rss = Mock(return_value="tailscale news")
    assert collector.collect_tailscale() == "tailscale news"
    collector._fetch_rss.assert_called_once_with(
        {"Tailscale": "https://tailscale.com/blog/index.xml"}, "TAILSCALE",
        max_per_feed=3, max_total=3,
    )
