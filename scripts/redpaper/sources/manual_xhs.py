"""Manual 小红书 source.

Users maintain `config/manual_xhs.json` with hand-picked posts. We don't
attempt to scrape Xiaohongshu (it has aggressive anti-bot defences); the user
just lists titles, URLs, optional covers, and channels.

Each post becomes a `Paper` with source="manual_xhs" and surfaces in the
regular feed, indistinguishable from arXiv cards.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path

from ..config import REPO_ROOT
from ..models import Author, Paper

log = logging.getLogger(__name__)


def _slug(url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"xhs-{h}"


def load_posts(path: Path | None = None) -> list[Paper]:
    """Read `manual_xhs.json` and convert to Paper objects."""
    path = path or (REPO_ROOT / "config" / "manual_xhs.json")
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("manual_xhs read failed: %s", e)
        return []

    out: list[Paper] = []
    today = dt.date.today().isoformat()
    for entry in data.get("posts", []):
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        title = (entry.get("title") or "小红书帖子").strip()
        out.append(
            Paper(
                id=_slug(url),
                source="manual_xhs",
                title=title,
                title_zh=title,
                abstract=entry.get("abstract", title),
                abstract_zh=entry.get("abstract", title),
                tldr_zh=entry.get("tldr", title)[:60],
                authors=[Author(name=entry.get("author") or "小红书")],
                published=entry.get("published", today),
                abs_url=url,
                pdf_url="",
                cover_image=entry.get("cover", ""),
                channels=entry.get("channels") or ["llm"],
            )
        )
    log.info("manual_xhs: loaded %d posts", len(out))
    return out
