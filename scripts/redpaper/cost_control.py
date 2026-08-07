"""Small persistent guards for recurring LLM work."""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Iterable


log = logging.getLogger(__name__)


def daily_slot_available(path: Path, slot: str) -> bool:
    """Return whether a costly once-per-day slot has not run today."""
    today = dt.date.today().isoformat()
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(slot) != today
    except Exception:
        return True


def mark_daily_slot(path: Path, slot: str) -> None:
    """Persist a successful costly slot claim for the current date."""
    data: dict = {}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data[slot] = dt.date.today().isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        log.warning("daily slot state write failed (%s): %s", slot, e)


def monthly_digest_is_current(digest_path: Path, month_papers: Iterable[object]) -> bool:
    """Whether the cached digest already covers exactly this paper set."""
    if not digest_path.exists():
        return False
    try:
        previous = json.loads(digest_path.read_text(encoding="utf-8"))
        previous_ids = set(previous.get("paper_ids") or [])
    except Exception:
        return False
    return previous_ids == {str(getattr(p, "id", "")) for p in month_papers}


__all__ = ["daily_slot_available", "mark_daily_slot", "monthly_digest_is_current"]
