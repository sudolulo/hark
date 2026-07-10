import json

import httpx

from hark.wikidata import Canonicalizer


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def make_canon(handler, **kwargs):
    kwargs.setdefault("delay", 0)
    return Canonicalizer(make_client(handler), **kwargs)


def search_response(items):
    return httpx.Response(200, content=json.dumps({"search": items}))


def test_canonicalize_returns_top_hit():
    def handler(request):
        assert request.url.params["search"] == "BTK"
        return search_response([{"id": "Q2295394", "label": "Dennis Rader"}])

    match = make_canon(handler).canonicalize("BTK")
    assert match.qid == "Q2295394"
    assert match.label == "Dennis Rader"


def test_canonicalize_no_hits_returns_none():
    canon = make_canon(lambda request: search_response([]))
    assert canon.canonicalize("zzz nonsense zzz") is None


def test_canonicalize_http_error_returns_none():
    canon = make_canon(lambda request: httpx.Response(500), retries=0)
    assert canon.canonicalize("Titanic") is None


def test_canonicalize_caches_case_insensitively():
    calls = []

    def handler(request):
        calls.append(request.url.params["search"])
        return search_response([{"id": "Q25173", "label": "Titanic"}])

    canon = make_canon(handler)
    assert canon.canonicalize("titanic").qid == "Q25173"
    assert canon.canonicalize("TITANIC").qid == "Q25173"
    assert len(calls) == 1


def test_canonicalize_retries_throttled_requests():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return search_response([{"id": "Q523020", "label": "Peter Sutcliffe"}])

    canon = make_canon(handler)
    assert canon.canonicalize("Peter Sutcliffe").qid == "Q523020"
    assert len(calls) == 2


def test_canonicalize_gives_up_after_retries():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "0"})

    canon = make_canon(handler, retries=2)
    assert canon.canonicalize("Anything") is None
    assert len(calls) == 3
