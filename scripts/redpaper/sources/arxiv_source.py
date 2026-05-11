"""arXiv source: fetch latest papers per channel."""
from __future__ import annotations

import datetime as dt
import logging
import re
import time
from typing import Iterable

import arxiv

from ..config import Channel, SourcesConfig
from ..models import Author, Paper

log = logging.getLogger(__name__)

_ARXIV_ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?$")


def _slug_from_arxiv(entry_id: str) -> str:
    # entry_id like 'http://arxiv.org/abs/2501.12345v2'
    m = _ARXIV_ID_RE.search(entry_id)
    if not m:
        return entry_id.rsplit("/", 1)[-1].replace(".", "-")
    return f"arxiv-{m.group(1).replace('.', '-')}"


def _matches_filters(title: str, abstract: str, channel: Channel) -> bool:
    text = f"{title}\n{abstract}".lower()
    if channel.exclude:
        for kw in channel.exclude:
            if kw.lower() in text:
                return False
    if not channel.keywords:
        return True
    return any(kw.lower() in text for kw in channel.keywords)


def _build_query(channel: Channel) -> str:
    cats = " OR ".join(f"cat:{c}" for c in channel.arxiv_categories)
    return f"({cats})" if cats else ""


def fetch_channel(channel: Channel, cfg: SourcesConfig) -> list[Paper]:
    """Pull recent arXiv papers matching this channel.

    The arxiv API doesn't filter by date directly, so we fetch the latest
    `per_channel_limit` papers (sorted by submission date desc) and then
    filter to the lookback window + keyword matches client-side.

    HTTP 429 from arXiv (common on shared CI IPs) is caught and logged;
    the channel just returns no results rather than aborting the pipeline.
    """
    query = _build_query(channel)
    if not query:
        return []

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg.arxiv_lookback_days)

    # Larger delay + more retries reduce 429s on GitHub Actions runners.
    client = arxiv.Client(page_size=50, delay_seconds=8.0, num_retries=5)
    search = arxiv.Search(
        query=query,
        max_results=cfg.arxiv_per_channel_limit,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )

    out: list[Paper] = []
    try:
        results_iter = client.results(search)
    except Exception as e:
        log.warning("arxiv[%s] search init failed: %s", channel.id, e)
        return []

    while True:
        try:
            result = next(results_iter)
        except StopIteration:
            break
        except arxiv.HTTPError as e:
            log.warning("arxiv[%s] HTTP %s: aborting this channel", channel.id, getattr(e, "status", "?"))
            break
        except Exception as e:
            log.warning("arxiv[%s] iter error: %s", channel.id, e)
            break
        published = result.published
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.timezone.utc)
        if published < cutoff:
            break  # results are date-desc, can stop early

        title = (result.title or "").strip().replace("\n", " ")
        abstract = (result.summary or "").strip().replace("\n", " ")
        if not _matches_filters(title, abstract, channel):
            continue

        arxiv_id_full = result.get_short_id()  # e.g. '2501.12345v2'
        arxiv_id_base = re.sub(r"v\d+$", "", arxiv_id_full)
        slug = f"arxiv-{arxiv_id_base.replace('.', '-')}"

        paper = Paper(
            id=slug,
            source="arxiv",
            title=title,
            abstract=abstract,
            authors=[Author(name=a.name) for a in result.authors],
            primary_category=result.primary_category or "",
            categories=list(result.categories or []),
            published=published.date().isoformat(),
            updated=result.updated.date().isoformat() if result.updated else "",
            arxiv_id=arxiv_id_base,
            pdf_url=result.pdf_url or "",
            abs_url=result.entry_id or "",
            channels=[channel.id],
        )
        out.append(paper)

    log.info("arxiv[%s]: fetched %d papers", channel.id, len(out))
    return out


def fetch_all(channels: Iterable[Channel], cfg: SourcesConfig) -> dict[str, Paper]:
    """Fetch for all channels and merge by paper id (one paper may appear in multiple channels)."""
    merged: dict[str, Paper] = {}
    channels = list(channels)
    for i, ch in enumerate(channels):
        for paper in fetch_channel(ch, cfg):
            if paper.id in merged:
                # union channels
                for c in paper.channels:
                    if c not in merged[paper.id].channels:
                        merged[paper.id].channels.append(c)
            else:
                merged[paper.id] = paper
        # Sleep between channels to ease arXiv rate limiting on shared IPs.
        if i + 1 < len(channels):
            time.sleep(10)
    return merged
