"""Manual arXiv source —— 「贴 arxiv 链接就上墙」.

Reads `config/manual_arxiv.yaml`, normalises each entry to a bare arXiv ID
(`2501.12345`), then calls the arXiv API to fetch full metadata (title,
authors, abstract, PDF URL, primary_category…). Each paper is emitted with
`source="manual_arxiv"` plus `source_tags=["manual_pin"]` so:

* the scoring rule `manual_pin` (config/scoring.yaml) fires for it
* the frontend can render a 📌 「精选」 badge
* the channel-retag step in build.py keeps it on the feed regardless of
  whether it currently matches channel keywords (user explicitly chose it)

Channels: if the user supplies `channels:` per-entry we honour them, otherwise
we run the same keyword matching as arxiv_source to assign whichever channels
fit. If nothing fits we drop it into the first channel in channels.yaml so
the paper still shows up under SOME tab.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Iterable

import arxiv
import yaml

from ..config import REPO_ROOT, Channel
from ..models import Author, Paper

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")
_OLD_ID_RE = re.compile(r"([a-z\-]+/\d{7})(v\d+)?", re.IGNORECASE)


def _extract_id(raw: str) -> str | None:
    """Pull an arXiv ID out of a URL / id / id+version string."""
    if not raw:
        return None
    s = str(raw).strip()
    # Try new-style id first (e.g. 2501.12345)
    m = _ID_RE.search(s)
    if m:
        return m.group(1)
    # Then old-style id (e.g. cs/0102000)
    m = _OLD_ID_RE.search(s)
    if m:
        return m.group(1)
    return None


def _slug(arxiv_id: str) -> str:
    return f"arxiv-{arxiv_id.replace('.', '-').replace('/', '-')}"


def _load_entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("manual_arxiv: read failed: %s", e)
        return []
    raw_list = data.get("papers") or []
    out: list[dict] = []
    for raw in raw_list:
        if isinstance(raw, str):
            aid = _extract_id(raw)
            if aid:
                out.append({"id": aid, "note": "", "channels": None})
        elif isinstance(raw, dict):
            aid = _extract_id(raw.get("id") or raw.get("url") or "")
            if aid:
                out.append({
                    "id": aid,
                    "note": (raw.get("note") or "").strip(),
                    "channels": raw.get("channels"),
                })
    return out


def _assign_channels(title: str, abstract: str, channels: list[Channel]) -> list[str]:
    """Same keyword-match logic as arxiv_source._matches_filters, but checks
    every channel and returns ALL that fit."""
    text = f"{title}\n{abstract}".lower()
    matches: list[str] = []
    for ch in channels:
        if ch.exclude and any(kw.lower() in text for kw in ch.exclude):
            continue
        if not ch.keywords:
            continue  # don't auto-assign to keyword-less channels
        if any(kw.lower() in text for kw in ch.keywords):
            matches.append(ch.id)
    return matches


def load_papers(channels: Iterable[Channel] | None = None, path: Path | None = None) -> list[Paper]:
    """Read config/manual_arxiv.yaml -> fetch each via arxiv API -> Papers."""
    path = path or (REPO_ROOT / "config" / "manual_arxiv.yaml")
    entries = _load_entries(path)
    if not entries:
        return []

    channels = list(channels or [])
    fallback_channel = channels[0].id if channels else "llm"

    # Batch-fetch all IDs in one arXiv call to be polite.
    ids = [e["id"] for e in entries]
    note_by_id = {e["id"]: e["note"] for e in entries}
    chan_override_by_id = {e["id"]: e["channels"] for e in entries}

    client = arxiv.Client(page_size=50, delay_seconds=5.0, num_retries=4)
    search = arxiv.Search(id_list=ids)

    out: list[Paper] = []
    try:
        results_iter = client.results(search)
    except Exception as e:
        log.warning("manual_arxiv: search init failed: %s", e)
        return []

    while True:
        try:
            r = next(results_iter)
        except StopIteration:
            break
        except Exception as e:
            log.warning("manual_arxiv: iter error: %s", e)
            break

        arxiv_id_full = r.get_short_id()  # 2501.12345v2
        arxiv_id_base = re.sub(r"v\d+$", "", arxiv_id_full)
        slug = _slug(arxiv_id_base)
        title = (r.title or "").strip().replace("\n", " ")
        abstract = (r.summary or "").strip().replace("\n", " ")

        # Pick channels: user override > keyword match > first config channel
        ch_override = chan_override_by_id.get(arxiv_id_base)
        if ch_override:
            chs = [str(c) for c in ch_override if c]
        else:
            chs = _assign_channels(title, abstract, channels) or [fallback_channel]

        paper = Paper(
            id=slug,
            source="manual_arxiv",
            title=title,
            abstract=abstract,
            authors=[Author(name=a.name) for a in r.authors],
            primary_category=r.primary_category or "",
            categories=list(r.categories or []),
            published=r.published.date().isoformat() if r.published else "",
            updated=r.updated.date().isoformat() if r.updated else "",
            arxiv_id=arxiv_id_base,
            pdf_url=r.pdf_url or "",
            abs_url=r.entry_id or "",
            channels=chs,
            source_tags=["manual_pin"],
        )
        # User-provided note becomes a related_link of source "note" if any.
        if note_by_id.get(arxiv_id_base):
            paper.related_links.append({
                "source": "note",
                "source_name": "站长注解",
                "title": note_by_id[arxiv_id_base],
                "url": paper.abs_url,
            })
        out.append(paper)

    log.info("manual_arxiv: loaded %d / %d entries", len(out), len(entries))
    if len(out) < len(entries):
        missing = set(ids) - {p.arxiv_id for p in out}
        log.warning("manual_arxiv: failed to resolve: %s", sorted(missing))
    return out
