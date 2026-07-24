from hark import db, series


def test_series_key_detects_part_markers():
    assert series.series_key("The Somerton Man - Part 2") == ("the somerton man", 2)
    assert series.series_key("Dyatlov Pass (Pt. 3)") == ("dyatlov pass", 3)
    assert series.series_key("A Cold Case (2 of 4)") == ("a cold case", 2)
    assert series.series_key("A standalone episode") is None
    assert series.series_key("") is None
    assert series.series_key(None) is None


def test_siblings_group_within_a_show_only(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO shows (query, title) VALUES ('a', 'Show A')")
    conn.execute("INSERT INTO shows (query, title) VALUES ('b', 'Show B')")
    conn.executemany(
        "INSERT INTO episodes (show_id, guid, title) VALUES (?, ?, ?)",
        [(1, 'a1', 'The Big Case - Part 1'),
         (1, 'a2', 'The Big Case - Part 2'),
         (1, 'a3', 'The Big Case - Part 3'),
         (1, 'a4', 'An unrelated episode'),
         (2, 'b1', 'The Big Case - Part 1')])   # same title, DIFFERENT show -> not a sibling
    conn.commit()

    sib = series.siblings(conn, 2)               # from Part 2
    assert [s["part"] for s in sib] == [1, 2, 3]
    assert {s["id"] for s in sib} == {1, 2, 3}   # only show A's parts, not show B's
    assert series.siblings(conn, 4) == []        # a standalone episode has no series
