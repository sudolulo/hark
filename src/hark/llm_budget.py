"""Per-category daily spend caps for the LLM tiers, so 'auto-enabled' can never become a
runaway invoice — and so ad detection and topic/comparison work never cannibalise each other's
budget.

There are two independent pools, each funded by its own env var and metered separately:

  - ADS          — `detect-ads` (ad-span classification from transcripts), `$HARK_LLM_ADS_BUDGET`
  - COMPARISONS  — the topic-index / treatment-comparison LLM work (`extract` today, a future
                   `compare` when it's built), `$HARK_LLM_COMPARISONS_BUDGET`

Each is dollars/day, default 0 = that pool stays OFF. A key alone never spends: you also fund the
specific pool you want on. That is the deliberate second switch, now per category so you can, say,
run ad-stripping hot while keeping topic extraction on a tight leash (or off) — they no longer
compete for one shared cap the way the pre-0.27 single `$HARK_LLM_DAILY_BUDGET` did.

Spend is ESTIMATED from the size of the prompt actually sent (chars/4 tokens, output negligible
against a per-1M input price), not read back from the API — an estimate is enough to STOP before
overspending, which is the only thing a cap has to get right, and it needs no usage plumbing
through adscrub. A per-(day, category) row keeps it simple and legible; caps reset at UTC midnight.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

# Opus 4.8 input price, $/1M tokens — the model hark's ad/topic extraction defaults to. Output is
# tiny for these tasks (span-index lists, short topic labels) so only input is metered.
INPUT_DOLLARS_PER_1M = 5.0
CHARS_PER_TOKEN = 4.0

# Budget categories.
ADS = "ads"
COMPARISONS = "comparisons"

# The env var that funds each category. First one that is SET wins, so the category-specific var
# takes precedence over the legacy shared knob.
_BUDGET_ENV: dict[str, tuple[str, ...]] = {
    ADS: ("HARK_LLM_ADS_BUDGET", "HARK_LLM_DAILY_BUDGET"),  # legacy shared knob still funds ads
    COMPARISONS: ("HARK_LLM_COMPARISONS_BUDGET",),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_spend (
    day      TEXT NOT NULL,      -- UTC date, YYYY-MM-DD
    category TEXT NOT NULL,      -- 'ads' | 'comparisons'
    dollars  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, category)
);
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_schema(conn: sqlite3.Connection) -> None:
    # Migrate the pre-0.27 single-budget table (PRIMARY KEY day, no category column). Its rows are
    # throwaway intra-day state that resets at UTC midnight anyway, so dropping it loses nothing
    # meaningful and avoids a fiddly ALTER + backfill of a synthetic category.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(llm_spend)").fetchall()]
    if cols and "category" not in cols:
        conn.execute("DROP TABLE llm_spend")
    conn.executescript(_SCHEMA)
    conn.commit()


def daily_cap(category: str) -> float:
    """Dollars/day for `category` from its env var(s). 0 (the default) means that pool is OFF —
    so a key alone doesn't spend; you also fund the specific pool. The category-specific var wins
    over the legacy `HARK_LLM_DAILY_BUDGET` when both are set."""
    for name in _BUDGET_ENV.get(category, ()):
        raw = os.environ.get(name)
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except ValueError:
                return 0.0
    return 0.0


def estimate_dollars(prompt_chars: int) -> float:
    return (prompt_chars / CHARS_PER_TOKEN) / 1_000_000 * INPUT_DOLLARS_PER_1M


def spent_today(conn: sqlite3.Connection, category: str) -> float:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT dollars FROM llm_spend WHERE day = ? AND category = ?", (_today(), category)
    ).fetchone()
    return row[0] if row else 0.0


def remaining(conn: sqlite3.Connection, category: str) -> float:
    """Dollars left in today's budget for `category`. <= 0 means stop. With no cap set, always 0
    (that pool is off)."""
    cap = daily_cap(category)
    return 0.0 if cap <= 0 else max(0.0, cap - spent_today(conn, category))


def record(conn: sqlite3.Connection, category: str, dollars: float) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO llm_spend (day, category, dollars) VALUES (?, ?, ?) "
        "ON CONFLICT(day, category) DO UPDATE SET dollars = dollars + excluded.dollars",
        (_today(), category, dollars),
    )
    conn.commit()
