import httpx

from hark import alert


def test_notify_is_a_noop_without_a_url(monkeypatch):
    monkeypatch.delenv("HARK_NTFY_URL", raising=False)
    assert alert.enabled() is False
    assert alert.notify("title", "message") is False        # dormant until the switch is flipped


def test_notify_posts_with_title_and_bearer_auth(monkeypatch):
    sent = {}

    def fake_post(url, content=None, headers=None, timeout=None):
        sent.update(url=url, content=content, headers=headers)
        return httpx.Response(200)

    monkeypatch.setenv("HARK_NTFY_URL", "https://ntfy.example/hark")
    monkeypatch.setenv("HARK_NTFY_TOKEN", "tok")
    monkeypatch.setattr(httpx, "post", fake_post)

    assert alert.enabled() is True
    assert alert.notify("Title", "Body") is True
    assert sent["url"] == "https://ntfy.example/hark"
    assert sent["content"] == b"Body"
    assert sent["headers"]["Title"] == "Title"
    assert sent["headers"]["Authorization"] == "Bearer tok"


def test_notify_swallows_errors(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("ntfy down")

    monkeypatch.setenv("HARK_NTFY_URL", "https://ntfy.example/hark")
    monkeypatch.setattr(httpx, "post", boom)
    assert alert.notify("t", "m") is False                  # best-effort: never raises


def test_notify_false_on_error_status(monkeypatch):
    monkeypatch.setenv("HARK_NTFY_URL", "https://ntfy.example/hark")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(403))
    assert alert.notify("t", "m") is False
