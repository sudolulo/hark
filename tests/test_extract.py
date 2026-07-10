from hark.extract import GENRES, ClaudeExtractor, _Extraction, _Topic


class StubMessages:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)

        class Response:
            parsed_output = self.parsed

        return Response()


class StubClient:
    def __init__(self, parsed):
        self.messages = StubMessages(parsed)


def test_extract_maps_topics():
    parsed = _Extraction(
        topics=[
            _Topic(label="Dennis Rader", genres=["true_crime"], confidence=0.95),
            _Topic(label="Wichita", genres=["history", "not_a_genre"], confidence=0.4),
        ]
    )
    ex = ClaudeExtractor(StubClient(parsed), model="claude-test")
    topics = ex.extract("BTK Part 1", "The killer who named himself.")
    assert [t.label for t in topics] == ["Dennis Rader", "Wichita"]
    assert topics[0].genres == ("true_crime",)
    assert topics[1].genres == ("history",)  # unknown genre dropped
    assert topics[0].confidence == 0.95


def test_extract_clamps_confidence_and_skips_blank_labels():
    parsed = _Extraction(
        topics=[
            _Topic(label="  ", genres=[], confidence=0.5),
            _Topic(label="Titanic", genres=["disaster"], confidence=1.7),
        ]
    )
    topics = ClaudeExtractor(StubClient(parsed)).extract("t", None)
    assert len(topics) == 1
    assert topics[0].label == "Titanic"
    assert topics[0].confidence == 1.0


def test_extract_refusal_returns_empty():
    assert ClaudeExtractor(StubClient(None)).extract("t", "d") == []


def test_extract_request_shape():
    client = StubClient(_Extraction(topics=[]))
    ClaudeExtractor(client, model="claude-test").extract("A Title", "A" * 10_000)
    call = client.messages.calls[0]
    assert call["model"] == "claude-test"
    assert call["output_format"] is _Extraction
    body = call["messages"][0]["content"]
    assert body.startswith("Title: A Title")
    assert len(body) <= 4000  # description capped
    assert ", ".join(GENRES) in call["system"]
