import sqlite3

import pytest

from hark import db


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_connect_creates_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"shows", "episodes", "topics", "topic_genres", "episode_topics"} <= tables


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "test.db"
    db.connect(path).close()
    db.connect(path).close()


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (show_id, guid) VALUES (999, 'x')")


def test_episode_guid_unique_per_show(conn):
    conn.execute("INSERT INTO shows (query) VALUES ('a')")
    conn.execute("INSERT INTO shows (query) VALUES ('b')")
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'ep-1')")
    # same guid on another show is fine
    conn.execute("INSERT INTO episodes (show_id, guid) VALUES (2, 'ep-1')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO episodes (show_id, guid) VALUES (1, 'ep-1')")


def test_utcnow_format():
    value = db.utcnow()
    assert len(value) == 20 and value.endswith("Z") and value[10] == "T"
