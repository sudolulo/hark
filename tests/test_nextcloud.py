import httpx

from hark import nextcloud


def make_client(payload, expected_path=None, expected_params=None):
    def handler(request):
        if expected_path is not None:
            assert request.url.path == expected_path
        if expected_params is not None:
            for k, v in expected_params.items():
                assert request.url.params[k] == v
        assert request.headers.get("authorization", "").startswith("Basic ")
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_current_subscriptions_is_add_minus_remove():
    payload = {
        "add": ["https://a.example/feed", "https://b.example/feed", "https://c.example/feed"],
        "remove": ["https://b.example/feed"],
        "timestamp": 123,
    }
    with make_client(payload, "/index.php/apps/gpoddersync/subscriptions") as client:
        result = nextcloud.current_subscriptions(client, "https://nc.example", ("u", "p"))
    assert result == ["https://a.example/feed", "https://c.example/feed"]


def test_fetch_episode_actions_returns_actions_and_cursor():
    payload = {
        "actions": [{"podcast": "https://a.example/feed", "episode": "https://a.example/e1.mp3",
                      "action": "PLAY", "timestamp": "2026-01-01T00:00:00+00:00"}],
        "timestamp": 999,
    }
    with make_client(payload, "/index.php/apps/gpoddersync/episode_action",
                      expected_params={"since": "0"}) as client:
        actions, since = nextcloud.fetch_episode_actions(client, "https://nc.example", ("u", "p"))
    assert len(actions) == 1
    assert since == 999


def test_fetch_episode_actions_passes_since_cursor():
    with make_client({"actions": [], "timestamp": 500},
                      expected_params={"since": "200"}) as client:
        nextcloud.fetch_episode_actions(client, "https://nc.example", ("u", "p"), since=200)
