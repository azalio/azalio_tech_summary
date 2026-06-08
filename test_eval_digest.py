"""Unit tests for eval_digest.py — pure parsing, no network/E5."""

import eval_digest as ed


INTEL = """ARXIV AI/ML PAPERS:
[cs.LG] Speculative decoding for cheaper serving - Link: http://arxiv.org/abs/2606.01234
[cs.CL] Pure theory of scaling laws - Link: http://arxiv.org/abs/2606.05678
HUGGINGFACE DAILY PAPERS (upvotes>=100):
- A practical RAG eval framework (240 upvotes) - Link: https://huggingface.co/papers/2606.09999
HACKER NEWS (front page):
- Some infra story (300 pts) - Link: https://news.ycombinator.com/item?id=1
"""

SUMMARY_KEEPS_ONE = """**🤖 AI / ML / LLM**
• DFlash ускоряет инференс LLM. [ArXiv](http://arxiv.org/abs/2606.01234)

**⚙️ DEVOPS / SRE / CLOUD**
• Что-то про Kubernetes. [HN](https://news.ycombinator.com/item?id=1)
"""

SUMMARY_NO_NEWS = "За последний час значимых новостей не зафиксировано."


def test_paper_key_arxiv_and_hf():
    assert ed.paper_key("http://arxiv.org/abs/2606.01234") == "arxiv:2606.01234"
    assert ed.paper_key("https://huggingface.co/papers/2606.09999") == "hf:2606.09999"
    assert ed.paper_key("https://example.com/foo") is None


def test_extract_paper_candidates():
    cands = ed.extract_paper_candidates(INTEL)
    assert set(cands) == {"arxiv:2606.01234", "arxiv:2606.05678", "hf:2606.09999"}
    assert "Speculative decoding" in cands["arxiv:2606.01234"]


def test_papers_kept_matches_by_url():
    cands = ed.extract_paper_candidates(INTEL)
    kept = ed.papers_kept_in_summary(SUMMARY_KEEPS_ONE, cands)
    assert kept == {"arxiv:2606.01234"}


def test_extract_aiml_section():
    body = ed.extract_aiml_section(SUMMARY_KEEPS_ONE)
    assert "DFlash" in body
    assert "Kubernetes" not in body  # stops at next section header


def test_count_items():
    assert ed.count_items(SUMMARY_KEEPS_ONE) == 2
    assert ed.count_items(SUMMARY_NO_NEWS) == 0


def test_analyze_run():
    rec = {"ts": "2026-06-08T10:00:00+00:00", "intelligence": INTEL, "summary": SUMMARY_KEEPS_ONE}
    r = ed.analyze_run(rec)
    assert r["paper_candidates"] == 3
    assert r["papers_kept"] == 1
    assert r["items"] == 2
    assert r["no_news"] is False
    assert r["has_aiml_section"] is True


def test_analyze_no_news_run():
    rec = {"ts": "t", "intelligence": INTEL, "summary": SUMMARY_NO_NEWS}
    r = ed.analyze_run(rec)
    assert r["no_news"] is True
    assert r["papers_kept"] == 0


def test_iter_records_skips_garbage():
    lines = ['{"a": 1}', "", "not json", '{"b": 2}']
    recs = list(ed.iter_records(lines))
    assert recs == [{"a": 1}, {"b": 2}]


def test_aggregate():
    runs = [
        ed.analyze_run({"ts": "1", "intelligence": INTEL, "summary": SUMMARY_KEEPS_ONE}),
        ed.analyze_run({"ts": "2", "intelligence": INTEL, "summary": SUMMARY_NO_NEWS}),
    ]
    report = ed.aggregate(runs)
    assert report["runs"] == 2
    assert report["no_news_runs"] == 1
    assert report["paper_candidates_total"] == 6
    assert report["papers_kept_total"] == 1
    assert 0.0 < report["paper_keep_rate"] < 1.0


def test_aggregate_empty():
    assert ed.aggregate([]) == {"runs": 0}
