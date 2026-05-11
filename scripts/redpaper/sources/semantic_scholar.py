"""Semantic Scholar enrichment.

Looks up citation counts and TL;DR text for arXiv papers via the public Graph
API. The public endpoint is rate-limited but no key is required.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger(__name__)

SS_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"
USER_AGENT = "redpaper/0.1 (+https://github.com/Nangongyeee/redpaper)"


@dataclass
class SSInfo:
    citation_count: int = 0
    tldr_en: str = ""
    influential_count: int = 0


def fetch_batch(arxiv_ids: Iterable[str]) -> dict[str, SSInfo]:
    """Look up a list of arXiv IDs in one batch call.

    Returns map: arxiv_id (no version) -> SSInfo. Missing entries are omitted.
    """
    ids = [f"ARXIV:{aid.split('v')[0]}" for aid in arxiv_ids if aid]
    if not ids:
        return {}
    out: dict[str, SSInfo] = {}
    # Semantic Scholar batch caps at 500 ids per request.
    for i in range(0, len(ids), 100):
        chunk = ids[i : i + 100]
        try:
            r = requests.post(
                SS_BATCH_URL,
                params={"fields": "citationCount,influentialCitationCount,tldr"},
                json={"ids": chunk},
                timeout=30,
                headers={"User-Agent": USER_AGENT},
            )
            if r.status_code == 429:
                log.warning("semantic_scholar: rate limited, sleeping 20s")
                time.sleep(20)
                continue
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("semantic_scholar batch failed: %s", e)
            continue

        for sent_id, entry in zip(chunk, data):
            if not entry:
                continue
            aid = sent_id.replace("ARXIV:", "")
            tldr_text = ""
            if entry.get("tldr") and isinstance(entry["tldr"], dict):
                tldr_text = (entry["tldr"].get("text") or "").strip()
            out[aid] = SSInfo(
                citation_count=int(entry.get("citationCount") or 0),
                influential_count=int(entry.get("influentialCitationCount") or 0),
                tldr_en=tldr_text,
            )
        # Be polite.
        time.sleep(1.0)
    log.info("semantic_scholar: enriched %d papers", len(out))
    return out
