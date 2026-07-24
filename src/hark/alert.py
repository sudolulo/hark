"""Opt-in ntfy alerting for the pipeline.

Dormant unless `HARK_NTFY_URL` is set — the same 'a switch you deliberately flip' pattern as the
LLM budget and API key: the container ships with it off, and one env var turns it on. Optional
`HARK_NTFY_TOKEN` adds bearer auth for a protected topic.

`notify` NEVER raises: an alert channel failing must not take down the loop it is reporting on,
so every error (unset URL, network, auth, bad status) resolves to a quiet False.
"""
from __future__ import annotations

import os

import httpx


def enabled() -> bool:
    return bool(os.environ.get("HARK_NTFY_URL"))


def notify(title: str, message: str, priority: str = "high") -> bool:
    """POST an ntfy notification if HARK_NTFY_URL is set. Returns True iff it was accepted.
    Swallows everything otherwise — best-effort by design."""
    url = os.environ.get("HARK_NTFY_URL")
    if not url:
        return False
    headers = {"Title": title, "Priority": priority}
    token = os.environ.get("HARK_NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = httpx.post(url, content=message.encode("utf-8"), headers=headers, timeout=10.0)
        return resp.status_code < 400
    except Exception:  # noqa: BLE001 — alerting is best-effort; never propagate to the loop
        return False
