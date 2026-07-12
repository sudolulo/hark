import pytest

from hark import opml

SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
<head><title>Test</title></head>
<body>
<outline text="History" title="History">
  <outline type="rss" text="Show A" title="Show A" xmlUrl="https://a.example/feed.rss"/>
  <outline type="rss" text="Show B" xmlUrl="https://b.example/feed.rss"/>
</outline>
</body>
</opml>"""


def test_parse_opml_extracts_feed_urls_and_titles():
    entries = opml.parse_opml(SAMPLE)
    assert entries == [
        opml.OpmlEntry(feed_url="https://a.example/feed.rss", title="Show A"),
        opml.OpmlEntry(feed_url="https://b.example/feed.rss", title="Show B"),
    ]


def test_parse_opml_skips_folder_outlines_without_xmlurl():
    # The top-level "History" grouping outline has no xmlUrl — not a feed.
    entries = opml.parse_opml(SAMPLE)
    assert all(e.feed_url.startswith("https://") for e in entries)
    assert len(entries) == 2


def test_read_opml_file(tmp_path):
    path = tmp_path / "feeds.opml"
    path.write_text(SAMPLE)
    entries = opml.read_opml_file(path)
    assert len(entries) == 2


def test_parse_opml_malformed_xml_raises():
    import xml.etree.ElementTree as ET
    with pytest.raises(ET.ParseError):
        opml.parse_opml("<not valid xml")
