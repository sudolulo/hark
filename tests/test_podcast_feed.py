"""Function-level tests for the feed builders — specifically cut vs 'chapters' (mark, don't cut)
mode. The token-gated HTTP routes are covered in test_podcast_feed_routes.py; these exercise
build_recommendation_feed directly, since driving it through the route would require the full
scoring pipeline to pick the episode_ids.
"""
import xml.etree.ElementTree as ET

from hark import db, podcast_feed

TOKEN = "tok-rec-123"
_CHAPTERS_TAG = "{https://podcastindex.org/namespace/1.0}chapters"


def _enclosure_urls(rss: bytes) -> list[str | None]:
    return [e.get("url") for e in ET.fromstring(rss).iter("enclosure")]


def _chapters_urls(rss: bytes) -> list[str | None]:
    return [c.get("url") for c in ET.fromstring(rss).iter(_CHAPTERS_TAG)]


def _setup(tmp_path, cut_mode):
    conn = db.connect(tmp_path / "hark.db")
    conn.execute("INSERT INTO shows (query, title, feed_token, cut_mode) VALUES ('q','Show A',?,?)",
                 (TOKEN, cut_mode))
    cut = tmp_path / "cut2.mp3"
    cut.write_bytes(b"cut-bytes")
    conn.execute("INSERT INTO episodes (show_id, guid, title, audio_url, cut_path) "
                 "VALUES (1,'ep-2','Ep 2','http://orig/ep2.mp3',?)", (str(cut),))
    conn.execute("INSERT INTO ad_segments (episode_id, start_second, end_second, source) "
                 "VALUES (1, 30, 60, 'llm')")   # episode id 1, one cuttable ad span
    conn.commit()
    return conn


def test_recommendation_feed_cut_mode_serves_the_cut(tmp_path):
    conn = _setup(tmp_path, "cut")
    rss = podcast_feed.build_recommendation_feed(conn, "holden", "http://h:8710", [1], "rec-token")
    assert f"http://h:8710/audio/1/{TOKEN}.mp3" in _enclosure_urls(rss)   # local cut
    assert _chapters_urls(rss) == []                                      # no chapters in cut mode


def test_recommendation_feed_honors_chapters_mode(tmp_path):
    # the regression this fixes: a show set to "mark, don't cut" was still hard-cut in the
    # cross-show recommendation feed. Now it serves the ORIGINAL audio + a chapters link.
    conn = _setup(tmp_path, "chapters")
    rss = podcast_feed.build_recommendation_feed(conn, "holden", "http://h:8710", [1], "rec-token")
    assert "http://orig/ep2.mp3" in _enclosure_urls(rss)                  # original, not the cut
    assert f"http://h:8710/chapters/1/{TOKEN}.json" in _chapters_urls(rss)
