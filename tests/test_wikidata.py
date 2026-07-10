import json

import httpx

from hark.wikidata import Canonicalizer


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def search_response(items):
    return httpx.Response(200, content=json.dumps({"search": items}))


def test_canonicalize_returns_top_hit():
    def handler(request):
        assert request.url.params["search"] == "BTK"
        return search_response([{"id": "Q2295394", "label": "Dennis Rader"}])

    canon = Canonicalizer(make_client(handler))
    match = canon.canonicalize("BTK")
    assert match.qid == "Q2295394"
    assert match.label == "Dennis Rader"


def test_canonicalize_no_hits_returns_none():
    canon = Canonicalizer(make_client(lambda request: search_response([])))
    assert canon.canonicalize("zzz nonsense zzz") is None


def test_canonicalize_http_error_returns_none():
    canon = Canonicalizer(make_client(lambda request: httpx.Response(500)))
    assert canon.canonicalize("Titanic") is None


def test_canonicalize_caches_case_insensitively():
    calls = []

    def handler(request):
        calls.append(request.url.params["search"])
        return search_response([{"id": "Q25173", "label": "Titanic"}])

    canon = Canonicalizer(make_client(handler))
    assert canon.canonicalize("titanic").qid == "Q25173"
    assert canon.canonicalize("TITANIC").qid == "Q25173"
    assert len(calls) == 1
