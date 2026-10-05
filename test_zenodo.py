"""Zenodo collection tests — mocked HTTP, no DB/E5 or network."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from collectors import Collectors


def record(rid=1, title="Kubernetes service reliability", **changes):
    item = {
        "id": rid,
        "created": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "title": title,
            "description": "<p>Run <b>Kubernetes</b> &amp; monitor services.</p>",
            "keywords": ["Kubernetes"],
            "resource_type": {"type": "software"},
        },
    }
    item.update(changes)
    return item


@pytest.fixture
def collector():
    c = object.__new__(Collectors)
    c.candidates = []
    c.source_counts = {}
    c._is_seen = Mock(return_value=False)
    c._mark_seen = Mock()
    c._is_semantic_dup = Mock(return_value=False)
    return c


def serve(monkeypatch, pages):
    responses = []
    for page in pages:
        if isinstance(page, Exception):
            responses.append(page)
        else:
            response = Mock()
            response.json.return_value = {"hits": {"hits": page}}
            responses.append(response)
    get = Mock(side_effect=responses)
    monkeypatch.setattr("collectors.requests.get", get)
    return get


@pytest.mark.parametrize("title", [
    "Kubernetes manifests", "DevOps pipelines", "Site reliability engineering",
    "Infrastructure as code", "AI coding agent diff sentinel", "LLM inference",
    "Model serving with vLLM", "Cloud observability with OpenTelemetry",
])
def test_relevant_topics(title):
    assert Collectors._zenodo_is_relevant(title, [])


@pytest.mark.parametrize("title", [
    "Phenology observability in forests", "Quantum observability theory",
    "Generic AI agents for biology", "Inference of chemical reaction rates",
    "Financial infrastructure observability", "A new AI dataset",
])
def test_irrelevant_topics(title):
    assert not Collectors._zenodo_is_relevant(title, [])


def test_keywords_can_supply_topic():
    assert Collectors._zenodo_is_relevant("An operational tool", ["site reliability engineering"])


def test_emission_context_and_bookkeeping(collector, monkeypatch):
    item = record()
    item["metadata"]["related_identifiers"] = [
        {"identifier": "https://github.com/example/tool", "scheme": "url"},
        {"identifier": "https://[malformed", "scheme": "url"},
    ]
    item["metadata"]["description"] += "<script>bad()</script>"
    get = serve(monkeypatch, [[item]])
    out = collector.collect_zenodo()
    assert "Link: https://zenodo.org/records/1" in out
    assert "Run Kubernetes & monitor services." in out
    assert "<p>" not in out and "bad()" not in out
    assert "https://github.com/example/tool" in out
    assert "software" in out
    assert len(collector.candidates) == 1
    assert collector.source_counts == {"Zenodo": 1}
    assert collector.candidates[0].engagement is None
    collector._mark_seen.assert_called_once_with("https://zenodo.org/records/1", "Zenodo")
    params = get.call_args.kwargs["params"]
    assert params["size"] == 25 and params["all_versions"] == "false"
    assert "created:[" in params["q"]
    assert get.call_args.kwargs["timeout"] == 15


@pytest.mark.parametrize("kind", ["software", "dataset", "article", "preprint", "conferencepaper", "workingpaper", "report"])
def test_accepted_types(collector, monkeypatch, kind):
    item = record()
    item["metadata"]["resource_type"] = (
        {"type": kind} if kind in {"software", "dataset"}
        else {"type": "publication", "subtype": kind}
    )
    serve(monkeypatch, [[item]])
    assert collector.collect_zenodo()


@pytest.mark.parametrize("kind", ["peerreview", "book", "presentation", "poster", "other", ""])
def test_rejected_types_never_touch_dedup(collector, monkeypatch, kind):
    item = record()
    item["metadata"]["resource_type"] = {"type": "publication", "subtype": kind}
    serve(monkeypatch, [[item]])
    assert collector.collect_zenodo() == ""
    collector._mark_seen.assert_not_called()
    collector._is_semantic_dup.assert_not_called()


def test_stale_and_irrelevant_do_not_touch_state(collector, monkeypatch):
    old = record(created=(datetime.now(timezone.utc) - timedelta(days=9)).isoformat())
    noise = record(title="Quantum observability", metadata={
        "title": "Quantum observability", "resource_type": {"type": "software"},
    })
    serve(monkeypatch, [[old, noise]])
    assert collector.collect_zenodo() == ""
    collector._mark_seen.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"metadata": None}, {"metadata": []}, {"created": None}, {"created": "bad"},
    {"id": None}, {"id": "not-an-id"}, {"metadata": {"title": []}},
    {"metadata": {"title": "Kubernetes", "resource_type": []}},
])
def test_bad_record_does_not_lose_valid_record(collector, monkeypatch, changes):
    serve(monkeypatch, [[record(**changes), record(2)]])
    assert "records/2" in collector.collect_zenodo()
    assert len(collector.candidates) == 1


def test_seen_and_semantic_duplicates(collector, monkeypatch):
    collector._is_seen.side_effect = [True, False, False]
    collector._is_semantic_dup.side_effect = [True, False]
    serve(monkeypatch, [[record(1), record(2), record(3)]])
    out = collector.collect_zenodo()
    assert "records/3" in out and "records/1" not in out and "records/2" not in out
    assert len(collector.candidates) == 1


def test_page_budget_and_old_record_not_a_cutoff(collector, monkeypatch):
    old = record(created=(datetime.now(timezone.utc) - timedelta(days=9)).isoformat())
    get = serve(monkeypatch, [[old] * 25, [old] * 24 + [record(2)], [old] * 25])
    assert "records/2" in collector.collect_zenodo()
    assert get.call_count == 3
    assert [call.kwargs["params"]["page"] for call in get.call_args_list] == [1, 2, 3]


def test_emission_cap(collector, monkeypatch):
    get = serve(monkeypatch, [[record(i) for i in range(1, 26)]])
    collector.collect_zenodo()
    assert len(collector.candidates) == 8
    assert get.call_count == 1


def test_empty_feed(collector, monkeypatch):
    get = serve(monkeypatch, [[]])
    assert collector.collect_zenodo() == ""
    assert get.call_count == 1


@pytest.mark.parametrize("failure", [requests.Timeout("timeout"), ValueError("bad JSON")])
def test_failure_is_empty(collector, monkeypatch, failure):
    serve(monkeypatch, [failure])
    assert collector.collect_zenodo() == ""


def test_partial_failure_preserves_items(collector, monkeypatch):
    collector._is_seen.side_effect = [False] + [True] * 24
    serve(monkeypatch, [[record(i) for i in range(1, 26)], requests.Timeout("timeout")])
    assert "records/1" in collector.collect_zenodo()
    assert len(collector.candidates) == 1


@pytest.mark.parametrize("payload", [None, [], {}, {"hits": []}, {"hits": {"hits": None}}])
def test_bad_envelope_is_empty(collector, monkeypatch, payload):
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr("collectors.requests.get", Mock(return_value=response))
    assert collector.collect_zenodo() == ""


def test_http_failure_is_empty(collector, monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("503")
    monkeypatch.setattr("collectors.requests.get", Mock(return_value=response))
    assert collector.collect_zenodo() == ""
