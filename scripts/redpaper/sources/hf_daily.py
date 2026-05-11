"""Hugging Face Daily Papers source.

Fetches the daily-papers index page (the public JSON endpoint that
huggingface.co/papers uses) and returns a list of arXiv IDs with their
"upvote" counts. We don't make these into separate Papers; instead we use
them to enrich arXiv-sourced papers with hotness badges.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

HF_API_URL = "https://huggingface.co/api/daily_papers"


@dataclass
class HFDailyEntry:
    arxiv_id: str
    title: str
    upvotes: int
    published: str  # YYYY-MM-DD


def fetch_recent(lookback_days: int = 3, limit: int = 200) -> list[HFDailyEntry]:
    """Pull the recent HF Daily Papers entries.

    The endpoint accepts `?date=YYYY-MM-DD` for a specific day. We iterate over
    the last `lookback_days` days.
    """
    out: list[HFDailyEntry] = []
    today = dt.date.today()
    seen: set[str] = set()
    for delta in range(lookback_days):
        day = (today - dt.timedelta(days=delta)).isoformat()
        try:
            r = requests.get(
                HF_API_URL,
                params={"date": day, "limit": limit},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("hf_daily fetch failed for %s: %s", day, e)
            continue

        for item in data or []:
            paper = item.get("paper") or {}
            arxiv_id = paper.get("id") or item.get("id")
            if not arxiv_id or arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            out.append(
                HFDailyEntry(
                    arxiv_id=arxiv_id,
                    title=(paper.get("title") or item.get("title") or "").strip(),
                    upvotes=int(paper.get("upvotes") or item.get("upvotes") or 0),
                    published=day,
                )
            )
    log.info("hf_daily: %d unique papers across %d days", len(out), lookback_days)
    return out


def build_index(entries: list[HFDailyEntry]) -> dict[str, HFDailyEntry]:
    """Index by normalized arxiv_id (no version) -> entry."""
    out: dict[str, HFDailyEntry] = {}
    for e in entries:
        key = e.arxiv_id.split("v")[0]  # drop trailing v1/v2
        if key not in out or e.upvotes > out[key].upvotes:
            out[key] = e
    return out
