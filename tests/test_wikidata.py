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


def test_canonicalize_retries_http_date_retry_after(monkeypatch):
    from hark import wikidata

    calls = []
    sleeps = []
    monkeypatch.setattr(wikidata.time, "sleep", lambda s: sleeps.append(s))

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            # RFC 7231 permits an HTTP-date here, not just delta-seconds.
            return httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"})
        return search_response([{"id": "Q1", "label": "Whatever"}])

    canon = make_canon(handler)
    match = canon.canonicalize("whatever")
    assert match.qid == "Q1"
    assert len(calls) == 2
    assert sleeps == [wikidata.MAX_BACKOFF]  # far-future date clamps to the cap, doesn't crash


def test_canonicalize_retries_transport_errors(monkeypatch):
    from hark import wikidata

    monkeypatch.setattr(wikidata.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom", request=request)
        return search_response([{"id": "Q99", "label": "Recovered"}])

    canon = make_canon(handler)
    match = canon.canonicalize("flaky")
    assert match.qid == "Q99"
    assert len(calls) == 2
