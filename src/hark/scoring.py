"""M4: episode interestingness — pure computation over data hark already has
(listen_actions, episode_topics, topic_genres) plus ratings.py's external
show-rating cache. No LLM calls anywhere in this module; "interesting" here
is arithmetic, not judgment, which is both the cheapest possible answer and
the most auditable one — every component of a score is a named, inspectable
number, not a black-box blend.

Two kinds of signal, both keyed off the user's own listening history:
- Personal affinity: for genres and topics the user has actually engaged
  with (a real 'play' action, not just a download), their own completion
  ratio (position/total) is Bayesian-shrunk toward their overall average —
  see _shrink()'s own docstring for why one shrinkage formula covers both
  this and the external-rating signal below, instead of a hand-rolled
  minimum-sample-size cutoff living next to a separate weighted average.
- External rating: show_ratings' cached rating_avg/rating_count (ratings.py),
  similarly shrunk toward the mean rating across all rated shows.

recommended_episodes() combines whichever of these are actually present for
a given (not-yet-played) episode as a weighted average, renormalized to the
present subset — a user with no listening history at all collapses cleanly
to pure external-rating ranking, with no special-cased "new user" branch.
"""

from __future__ import annotations

import sqlite3

GENRE_AFFINITY_K = 5
TOPIC_AFFINITY_K = 5
EXTERNAL_RATING_K = 20

# topic:genre:external — the more specific/personal a signal, the more it's
# trusted when present.
_COMPONENT_WEIGHTS = {"topic": 3, "genre": 2, "external": 1}

_GENRE_GROUP_QUERY = """
    SELECT DISTINCT et.episode_id AS episode_id, tg.genre AS group_key
    FROM episode_topics et
    JOIN topic_genres tg ON tg.topic_id = et.topic_id
    WHERE et.episode_id IN ({placeholders})
"""

_TOPIC_GROUP_QUERY = """
    SELECT et.episode_id AS episode_id, et.topic_id AS group_key
    FROM episode_topics et
    WHERE et.episode_id IN ({placeholders})
"""


def _shrink(raw_by_key: dict, n_by_key: dict, prior: float, k: float) -> dict:
    """Bayesian shrinkage toward `prior`, weighted by each key's own sample
    size: (n/(n+k))*raw + (k/(n+k))*prior. At n=0 this already reduces to
    `prior` with no special case needed — the one real trap this avoids is
    a topic/genre/show with only 1-2 data points producing a near-0-or-1
    ratio that swamps the ranking; shrinking toward a broader prior instead
    of gating on a minimum sample size means there's one formula shape used
    everywhere a small sample needs tempering, not a threshold guard living
    next to a separately-invented Bayesian average for just one signal."""
    return {
        key: (n_by_key[key] / (n_by_key[key] + k)) * raw + (k / (n_by_key[key] + k)) * prior
        for key, raw in raw_by_key.items()
    }


def _completion_ratios(conn: sqlite3.Connection, user_id: int) -> dict[int, float]:
    """episode_id -> completion ratio (0-1), from the MAX(position/total)
    across any pause/resume 'play' rows for that episode — not an average.
    Only episodes with a real 'play' action count; a 'download'/'new' action
    with no play can't be told apart from "haven't gotten to it yet" versus
    "not interested", so it contributes no signal either way.

    Joins listen_actions to episodes via the show resolved from
    podcast_url = shows.feed_url, then episode_guid (scoped to that show,
    since GUIDs aren't globally unique — same scoping concern
    claims._group_transcripts_by_show documents for show *names*), with
    audio_url as a fallback — not raw string equality against
    listen_actions.episode_url alone, which would miss whenever a feed's
    reported audio URL includes tracking/redirect params that drifted
    between what AntennaPod recorded and what hark itself stored.
    """
    rows = conn.execute(
        """
        SELECT podcast_url, episode_url, episode_guid, position, total
        FROM listen_actions
        WHERE user_id = ? AND action = 'play'
          AND position IS NOT NULL AND total IS NOT NULL AND total > 0
        """,
        (user_id,),
    ).fetchall()
    if not rows:
        return {}

    show_by_feed_url = {
        r["feed_url"]: r["id"]
        for r in conn.execute("SELECT id, feed_url FROM shows WHERE feed_url IS NOT NULL")
    }
    episodes_by_show: dict[int, dict] = {}

    def episode_index(show_id: int) -> dict:
        if show_id not in episodes_by_show:
            eps = conn.execute(
                "SELECT id, guid, audio_url FROM episodes WHERE show_id = ?", (show_id,)
            ).fetchall()
            episodes_by_show[show_id] = {
                "by_guid": {e["guid"]: e["id"] for e in eps if e["guid"]},
                "by_audio_url": {e["audio_url"]: e["id"] for e in eps if e["audio_url"]},
            }
        return episodes_by_show[show_id]

    ratios: dict[int, float] = {}
    for row in rows:
        show_id = show_by_feed_url.get(row["podcast_url"])
        if show_id is None:
            continue
        index = episode_index(show_id)
        episode_id = index["by_guid"].get(row["episode_guid"]) if row["episode_guid"] else None
        if episode_id is None:
            episode_id = index["by_audio_url"].get(row["episode_url"])
        if episode_id is None:
            continue
        ratio = max(0.0, min(1.0, row["position"] / row["total"]))
        ratios[episode_id] = max(ratios.get(episode_id, 0.0), ratio)
    return ratios


def _completion_by_group(
    conn: sqlite3.Connection, ratios: dict[int, float], group_query: str
) -> tuple[dict, dict]:
    """Groups completion ratios by whatever `group_query` selects
    (episode_id, group_key) pairs for — genre or topic_id. An episode
    belonging to multiple groups (a show can cover 2+ genres; an episode
    can rarely cover 2+ topics) contributes its ratio to each. Returns
    (raw_average_by_group, play_count_by_group)."""
    if not ratios:
        return {}, {}
    placeholders = ",".join("?" * len(ratios))
    rows = conn.execute(
        group_query.format(placeholders=placeholders), list(ratios)
    ).fetchall()
    sums: dict = {}
    counts: dict = {}
    for row in rows:
        key = row["group_key"]
        ratio = ratios[row["episode_id"]]
        sums[key] = sums.get(key, 0.0) + ratio
        counts[key] = counts.get(key, 0) + 1
    return {k: sums[k] / counts[k] for k in sums}, counts


def _affinity_from_ratios(
    conn: sqlite3.Connection, ratios: dict[int, float], group_query: str, k: float
) -> dict | None:
    if not ratios:
        return None  # true cold start: no baseline at all to shrink toward
    user_mean = sum(ratios.values()) / len(ratios)
    raw, n = _completion_by_group(conn, ratios, group_query)
    return _shrink(raw, n, user_mean, k)


def genre_affinity(conn: sqlite3.Connection, user_id: int) -> dict[str, float] | None:
    """Shrunk average completion ratio per genre (only 8 buckets total) —
    the broad, almost-always-populated proxy for taste once a user has
    played anything at all. None only for zero played episodes."""
    return _affinity_from_ratios(conn, _completion_ratios(conn, user_id), _GENRE_GROUP_QUERY, GENRE_AFFINITY_K)


def topic_affinity(conn: sqlite3.Connection, user_id: int) -> dict[int, float] | None:
    """Same idea one level more specific. Topics are one-off real-world
    subjects (the Dyatlov Pass incident doesn't recur), so most will rarely
    have more than one play and will mostly shrink close to the genre-level
    number — expected, not a bug. Only pulls meaningfully ahead of
    genre_affinity when a user has engaged with the *same* subject more than
    once (a multi-part case, or two shows covering the same story)."""
    return _affinity_from_ratios(conn, _completion_ratios(conn, user_id), _TOPIC_GROUP_QUERY, TOPIC_AFFINITY_K)


def _weighted_external_ratings(conn: sqlite3.Connection) -> dict[int, float]:
    """show_id -> Bayesian-weighted external rating, shrunk toward the mean
    rating_avg across all rated shows so a show with 2 five-star ratings
    doesn't outrank one with 10,000 reviews averaging 4.3. Only includes
    shows with an actual rating on file — no row, or a row recording a miss
    (see ratings.refresh_ratings), means "no external signal" (absent from
    this dict), not "rated exactly average".

    show_ratings is a table this feature itself adds — wrapped in
    try/except since App.db() (views.py) only ever holds a read-only
    connection that never runs schema setup, so on a freshly-upgraded
    deployment the table may not exist yet until some other hark command
    (any CLI command touching hark.db) has run at least once since the
    upgrade."""
    try:
        rows = conn.execute(
            "SELECT show_id, rating_avg, rating_count FROM show_ratings WHERE rating_avg IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    if not rows:
        return {}
    global_mean = sum(r["rating_avg"] for r in rows) / len(rows)
    return {
        r["show_id"]: (r["rating_count"] / (r["rating_count"] + EXTERNAL_RATING_K)) * r["rating_avg"]
        + (EXTERNAL_RATING_K / (r["rating_count"] + EXTERNAL_RATING_K)) * global_mean
        for r in rows
    }


def _topics_by_episode(conn: sqlite3.Connection, episode_ids: list[int]) -> dict[int, dict[int, list[str]]]:
    """episode_id -> {topic_id: [genre, ...]}."""
    if not episode_ids:
        return {}
    placeholders = ",".join("?" * len(episode_ids))
    rows = conn.execute(
        f"""
        SELECT et.episode_id, et.topic_id, tg.genre
        FROM episode_topics et
        LEFT JOIN topic_genres tg ON tg.topic_id = et.topic_id
        WHERE et.episode_id IN ({placeholders})
        """,
        episode_ids,
    ).fetchall()
    result: dict[int, dict[int, list[str]]] = {}
    for row in rows:
        genres = result.setdefault(row["episode_id"], {}).setdefault(row["topic_id"], [])
        if row["genre"]:
            genres.append(row["genre"])
    return result


def recommended_episodes(conn: sqlite3.Connection, user_id: int, limit: int = 15) -> list[dict]:
    """Ranked list of not-yet-played episodes by predicted interest. Each
    component (topic affinity, genre affinity, external rating) is included
    only when actually present for that episode; the combined score is a
    weighted average over whichever components are present, renormalized to
    that subset (see module docstring and _shrink() for why). Returns the
    raw component values alongside the combined score, not just the score —
    auditable, not a black box.

    Standalone convenience wrapper — see recommendations_for_user() if you
    also need genre_affinity/topic_affinity for the same user, which
    otherwise means recomputing _completion_ratios() from scratch here."""
    ratios = _completion_ratios(conn, user_id)
    genre_aff = _affinity_from_ratios(conn, ratios, _GENRE_GROUP_QUERY, GENRE_AFFINITY_K) or {}
    topic_aff = _affinity_from_ratios(conn, ratios, _TOPIC_GROUP_QUERY, TOPIC_AFFINITY_K) or {}
    return _rank_candidates(conn, genre_aff, topic_aff, set(ratios), limit)


def recommendations_for_user(conn: sqlite3.Connection, user_id: int, limit: int = 15) -> dict:
    """Everything view_notable needs for one user in one pass: the ranked
    recommendation list plus the genre/topic affinity dicts behind it (for
    the page's own "your top genres/topics" mirror) — computed from a single
    shared _completion_ratios() call instead of the three redundant ones
    calling recommended_episodes()/genre_affinity()/topic_affinity()
    separately would cost on every page view."""
    ratios = _completion_ratios(conn, user_id)
    genre_aff = _affinity_from_ratios(conn, ratios, _GENRE_GROUP_QUERY, GENRE_AFFINITY_K)
    topic_aff = _affinity_from_ratios(conn, ratios, _TOPIC_GROUP_QUERY, TOPIC_AFFINITY_K)
    recommended = _rank_candidates(conn, genre_aff or {}, topic_aff or {}, set(ratios), limit)
    return {"recommended": recommended, "genre_affinity": genre_aff, "topic_affinity": topic_aff}


def _rank_candidates(
    conn: sqlite3.Connection, genre_aff: dict, topic_aff: dict, played: set, limit: int
) -> list[dict]:
    external = _weighted_external_ratings(conn)

    rows = conn.execute(
        """
        SELECT e.id, e.title, e.pubdate, s.id AS show_id, COALESCE(s.title, s.query) AS show
        FROM episodes e JOIN shows s ON s.id = e.show_id
        ORDER BY e.id
        """
    ).fetchall()
    candidate_ids = [r["id"] for r in rows if r["id"] not in played]
    if not candidate_ids:
        return []
    topics_by_episode = _topics_by_episode(conn, candidate_ids)

    scored = []
    for row in rows:
        if row["id"] in played:
            continue
        topics = topics_by_episode.get(row["id"], {})
        topic_scores = [topic_aff[t] for t in topics if t in topic_aff]
        genre_scores = [genre_aff[g] for genres in topics.values() for g in genres if g in genre_aff]

        components = {}
        if topic_scores:
            components["topic"] = max(topic_scores)
        if genre_scores:
            components["genre"] = sum(genre_scores) / len(genre_scores)
        if row["show_id"] in external:
            components["external"] = external[row["show_id"]]
        if not components:
            continue  # no signal at all for this episode — nothing to rank it by

        total_weight = sum(_COMPONENT_WEIGHTS[k] for k in components)
        combined = sum(v * _COMPONENT_WEIGHTS[k] for k, v in components.items()) / total_weight
        scored.append({
            "episode_id": row["id"],
            "title": row["title"],
            "pubdate": row["pubdate"],
            "show_id": row["show_id"],
            "show": row["show"],
            "topic_affinity": components.get("topic"),
            "genre_affinity": components.get("genre"),
            "external_rating": components.get("external"),
            "score": combined,
        })
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]
