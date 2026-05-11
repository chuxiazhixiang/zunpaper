"""Chinese AI media: 量子位 / 机器之心 / 新智元.

For each source we fetch the recent article list, peek at each article for a
mention of an arXiv ID, and attach the article as a "related link" on that
paper. Articles without an arXiv mention become standalone news cards
(future Phase 2.5; for now we drop them).

The HTML/RSS endpoints are public, so no key needed. They are best-effort —
selectors will rot, and a failure of one source must not break the pipeline.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; redpaper/0.1; +https://github.com/Nangongyeee/redpaper)"
)
TIMEOUT = 30

ARXIV_ID_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv[:\s])\s*(\d{4}\.\d{4,5})", re.IGNORECASE)


@dataclass
class NewsArticle:
    source: str          # qbitai / jiqizhixin / synced_review
    source_name: str     # 量子位 / 机器之心 / 新智元
    title: str
    url: str
    arxiv_ids: list[str]
    published: str = ""


def _safe_get(url: str) -> str:
    try:
        r = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        log.debug("fetch %s failed: %s", url, e)
        return ""


def _extract_arxiv_ids(html: str) -> list[str]:
    ids = set()
    for m in ARXIV_ID_RE.finditer(html):
        ids.add(m.group(1))
    return sorted(ids)


def _scan_articles(article_urls: list[tuple[str, str, str]], source: str, source_name: str,
                   limit: int = 20) -> list[NewsArticle]:
    """Given (title, url, date) tuples, fetch each and look for arXiv mentions."""
    out: list[NewsArticle] = []
    for title, url, date in article_urls[:limit]:
        html = _safe_get(url)
        if not html:
            continue
        ids = _extract_arxiv_ids(html)
        if not ids:
            continue
        out.append(NewsArticle(source=source, source_name=source_name,
                               title=title, url=url, arxiv_ids=ids, published=date))
        time.sleep(0.5)
    return out


# Each fetcher returns a list of (title, url, date) tuples, then delegates to _scan_articles.

QBITAI_LIST = "https://www.qbitai.com/category/ai/feed"
QBITAI_ITEM_RE = re.compile(
    r"<item>.*?<title><!\[CDATA\[(.*?)\]\]></title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
    re.DOTALL,
)


def fetch_qbitai(limit: int = 20) -> list[NewsArticle]:
    feed = _safe_get(QBITAI_LIST)
    items = QBITAI_ITEM_RE.findall(feed)
    triples = [(t.strip(), u.strip(), d.strip()[:16]) for t, u, d in items]
    return _scan_articles(triples, "qbitai", "量子位", limit)


JIQI_LIST = "https://www.jiqizhixin.com/rss"
JIQI_ITEM_RE = re.compile(
    r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?<pubDate>(.*?)</pubDate>",
    re.DOTALL,
)


def fetch_jiqizhixin(limit: int = 20) -> list[NewsArticle]:
    feed = _safe_get(JIQI_LIST)
    items = JIQI_ITEM_RE.findall(feed)
    def _clean(s: str) -> str:
        s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s)
        return s.strip()
    triples = [(_clean(t), _clean(u), _clean(d)[:16]) for t, u, d in items]
    return _scan_articles(triples, "jiqizhixin", "机器之心", limit)


SYNCED_LIST = "https://www.aiera.com.cn/feed"  # 新智元 RSS (best guess)
SYNCED_ITEM_RE = JIQI_ITEM_RE


def fetch_synced(limit: int = 20) -> list[NewsArticle]:
    feed = _safe_get(SYNCED_LIST)
    items = SYNCED_ITEM_RE.findall(feed)
    def _clean(s: str) -> str:
        s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s)
        return s.strip()
    triples = [(_clean(t), _clean(u), _clean(d)[:16]) for t, u, d in items]
    return _scan_articles(triples, "synced_review", "新智元", limit)


def fetch_all_enabled(enabled: dict[str, bool], limit_per_source: int = 20) -> list[NewsArticle]:
    out: list[NewsArticle] = []
    if enabled.get("qbitai"):
        try:
            out.extend(fetch_qbitai(limit_per_source))
        except Exception as e:
            log.warning("qbitai failed: %s", e)
    if enabled.get("jiqizhixin"):
        try:
            out.extend(fetch_jiqizhixin(limit_per_source))
        except Exception as e:
            log.warning("jiqizhixin failed: %s", e)
    if enabled.get("synced_review"):
        try:
            out.extend(fetch_synced(limit_per_source))
        except Exception as e:
            log.warning("synced_review failed: %s", e)
    return out


def build_arxiv_index(articles: Iterable[NewsArticle]) -> dict[str, list[NewsArticle]]:
    """arxiv_id -> articles that mention it."""
    out: dict[str, list[NewsArticle]] = {}
    for a in articles:
        for aid in a.arxiv_ids:
            out.setdefault(aid, []).append(a)
    return out
