"""A daily spend cap for the LLM tiers, so 'auto-enabled' can never become a runaway invoice.

The pipeline's rule is ads-first, budget-capped: `detect-ads` runs first (campaign-bounded, so
usually a handful of episodes), then the topic-index LLM work spends whatever daily budget is
left. This module is the shared meter both consult.

Spend is ESTIMATED from the size of the prompt actually sent (chars/4 tokens, output negligible
against a per-1M input price), not read back from the API — an estimate is enough to STOP before
overspending, which is the only thing a cap has to get right, and it needs no usage plumbing
through adscrub. A per-day row keeps it simple and legible; the cap resets at UTC midnight.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

# Opus 4.8 input price, $/1M tokens — the model hark's ad/topic extraction defaults to. Output is
# tiny for these tasks (span-index lists, short topic labels) so only input is metered.
INPUT_DOLLARS_PER_1M = 5.0
CHARS_PER_TOKEN = 4.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_spend (
    day     TEXT PRIMARY KEY,   -- UTC date, YYYY-MM-DD
    dollars REAL NOT NULL DEFAULT 0
);
"""


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def daily_cap() -> float:
    """Dollars/day from $HARK_LLM_DAILY_BUDGET. 0 (the default) means the LLM tiers stay OFF —
    so a key alone doesn't spend; you also set a budget. That is the deliberate second switch."""
    try:
        return max(0.0, float(os.environ.get("HARK_LLM_DAILY_BUDGET", "0")))
    except ValueError:
        return 0.0


def estimate_dollars(prompt_chars: int) -> float:
    return (prompt_chars / CHARS_PER_TOKEN) / 1_000_000 * INPUT_DOLLARS_PER_1M


def spent_today(conn: sqlite3.Connection) -> float:
    ensure_schema(conn)
    row = conn.execute("SELECT dollars FROM llm_spend WHERE day = ?", (_today(),)).fetchone()
    return row[0] if row else 0.0


def remaining(conn: sqlite3.Connection) -> float:
    """Dollars left in today's budget. <= 0 means stop. With no cap set, always 0 (LLM off)."""
    cap = daily_cap()
    return 0.0 if cap <= 0 else max(0.0, cap - spent_today(conn))


def record(conn: sqlite3.Connection, dollars: float) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO llm_spend (day, dollars) VALUES (?, ?) "
        "ON CONFLICT(day) DO UPDATE SET dollars = dollars + excluded.dollars",
        (_today(), dollars),
    )
    conn.commit()
