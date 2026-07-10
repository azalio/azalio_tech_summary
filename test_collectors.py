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


# --- _watcha_is_relevant: news-slice filter ---
# Strings below are verbatim Watcha items seen in production (digest_runs.jsonl):
# the relevant ones must pass, the consumer/community noise must be dropped.
# _watcha_is_relevant is an instance method but reads no instance state, so we
# bind it unbound via the class to avoid running Collectors.__init__ (DB setup).
def _relevant(title, body=""):
    return Collectors._watcha_is_relevant(object.__new__(Collectors), title, body)


def test_watcha_relevant_keeps_daily_roundup():
    # "今日观猹丨…" daily news roundups are always kept (Manus/DeepSeek news).
    assert _relevant("今日观猹丨原始投资者拟 20 亿美元赎回 Manus，DS 识图全量上线") is True


def test_watcha_relevant_keeps_dev_and_model_signal():
    assert _relevant("词元跳动", "兼容多主流协议与智能路由的 AI 原生大模型 API 统一网关工作台") is True
    assert _relevant("TokenDance 首发上线 GLM-5.2，内测一周后开放") is True
    assert _relevant("第一次感受到了提示词注入的恐怖") is True  # 注入 = prompt injection
    assert _relevant("有人用过这家免费模型否？Agnes") is True   # 模型 = model


def test_watcha_relevant_drops_consumer_noise():
    assert _relevant("今天你卖的是什么腿？", "我曾经只想卖一只热乎的腿") is False
    assert _relevant("动物去哪儿", "基于真实世界的小动物旅行游戏") is False
    assert _relevant("开箱了观猹的端午大礼包！") is False
    assert _relevant("新人来报到啦！") is False
    assert _relevant("日子也是好起来了") is False
    assert _relevant("冒个泡") is False


# --- TC260 listing parser (tc260.org.cn, HTML scrape, no RSS) ---
# HTML mirrors the real server-rendered structure: div.hygd-c > div.item, each
# with an <a href="/portal/article/N/…"> title and a <span>YYYY-MM-DD</span>.
_TC260_HTML = """
<div class="main-cont"><div class="hygd-list"><div class="hygd-c">
  <div class="item">
    <a target="_blank" href="/portal/article/1/abc123">6项人工智能应用安全国家标准化指导性技术文件启动会在京召开</a>
    <span>2026-07-10</span>
  </div>
  <div class="item">
    <a target="_blank" href="/portal/article/1/def456">网络安全标准应用实践案例 |  供应链安全主题之七</a>
    <span>2026-06-29</span>
  </div>
  <div class="item">
    <a target="_blank" href="/portal/about/mgr">关于我们</a>
    <span>2026-06-01</span>
  </div>
  <div class="item">
    <span>2026-05-01</span>
  </div>
  <div class="item">
    <a target="_blank" href="/portal/article/3/nodate">工作组会议纪要</a>
  </div>
</div></div></div>
"""


def test_parse_tc260_listing_extracts_article_items():
    rows = Collectors._parse_tc260_listing(_TC260_HTML)
    # Non-article href (/portal/about/mgr) and the <a>-less item are dropped;
    # the two real articles plus the date-less article remain.
    assert [p for _, p, _ in rows] == [
        "/portal/article/1/abc123",
        "/portal/article/1/def456",
        "/portal/article/3/nodate",
    ]
    assert rows[0] == (
        "6项人工智能应用安全国家标准化指导性技术文件启动会在京召开",
        "/portal/article/1/abc123",
        "2026-07-10",
    )
    # Inner whitespace (incl. the literal "|" separator) is collapsed to single
    # spaces, and the date is normalized to zero-padded YYYY-MM-DD.
    assert rows[1][0] == "网络安全标准应用实践案例 | 供应链安全主题之七"
    assert rows[1][2] == "2026-06-29"
    # A date-less article is still returned, with an empty date string (the
    # collector's recency gate then skips it).
    assert rows[2][2] == ""


def test_parse_tc260_listing_empty_or_garbage():
    assert Collectors._parse_tc260_listing("") == []
    assert Collectors._parse_tc260_listing("<html><body>no items</body></html>") == []


def test_freshness_from_iso_recent_is_high():
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    assert Collectors._freshness_from_iso(recent, max_age_days=21) > 0.8


def test_freshness_from_iso_old_is_clamped_zero():
    old = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    assert Collectors._freshness_from_iso(old, max_age_days=21) == 0.0


def test_freshness_from_iso_missing_is_neutral():
    assert Collectors._freshness_from_iso("", max_age_days=21) == 0.5
    assert Collectors._freshness_from_iso("not-a-date", max_age_days=21) == 0.5
