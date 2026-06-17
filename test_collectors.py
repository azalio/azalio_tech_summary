"""Unit tests for collectors.py helpers — pure, no network/E5, runs anywhere.

Covers the two helpers introduced for the Watcha (观猹) collector:
``_is_recent`` (ISO-8601 recency gate, tolerant of a trailing ``Z``) and
``_flatten_richtext`` (TipTap/ProseMirror doc -> plain text)."""

from datetime import datetime, timedelta, timezone

from collectors import Collectors


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_is_recent_within_window():
    recent = _iso(datetime.now(timezone.utc) - timedelta(days=2))
    assert Collectors._is_recent(recent, days=4) is True


def test_is_recent_outside_window():
    old = _iso(datetime.now(timezone.utc) - timedelta(days=10))
    assert Collectors._is_recent(old, days=4) is False


def test_is_recent_handles_z_suffix_and_fractional_seconds():
    # The Watcha API returns e.g. "2026-06-15T08:06:36.294Z" — fromisoformat
    # cannot parse a bare "Z", so the helper must rewrite it to "+00:00".
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.294Z"
    )
    assert Collectors._is_recent(recent, days=1) is True


def test_is_recent_missing_or_garbage_is_false():
    assert Collectors._is_recent(None, days=4) is False
    assert Collectors._is_recent("", days=4) is False
    assert Collectors._is_recent("not-a-date", days=4) is False


def test_flatten_richtext_nested_paragraphs():
    # Shape mirrors a real Watcha post body.
    doc = {
        "content": [
            {"content": [{"text": "谁还在每天定闹钟抢能量？", "type": "text"}],
             "type": "paragraph"},
            {"content": [
                {"text": "AI 版支付宝", "type": "text"},
                {"text": "「阿宝」上线了", "type": "text"},
            ], "type": "paragraph"},
        ],
        "type": "doc",
    }
    out = Collectors._flatten_richtext(doc)
    assert out == "谁还在每天定闹钟抢能量？ AI 版支付宝 「阿宝」上线了"


def test_flatten_richtext_collapses_whitespace_and_handles_empty():
    assert Collectors._flatten_richtext(None) == ""
    assert Collectors._flatten_richtext({}) == ""
    messy = {"content": [{"text": "  a\n\nb  ", "type": "text"}], "type": "doc"}
    assert Collectors._flatten_richtext(messy) == "a b"
