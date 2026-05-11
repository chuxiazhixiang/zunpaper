"""Generate a Markdown daily digest and an RSS feed.

The Markdown file is written to `site/digest/YYYY-MM-DD.md` so a webhook (飞书
机器人 / Server 酱 / 邮件) can grab it. The RSS is written to `site/rss.xml`.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
from pathlib import Path
from typing import Iterable

from .config import DATA_DIR, SITE_DIR
from .models import Paper

log = logging.getLogger(__name__)

DIGEST_DIR = SITE_DIR / "digest"
RSS_PATH = SITE_DIR / "rss.xml"
SITE_URL = "https://Nangongyeee.github.io/redpaper"


def _md_paper(p: Paper) -> str:
    title = p.title_zh or p.title
    badges = " ".join(b.get("label", "") for b in (p.badges or []))
    authors = "、".join(a.name for a in p.authors[:3])
    abs_url = p.abs_url or (f"https://arxiv.org/abs/{p.arxiv_id}" if p.arxiv_id else "")
    parts = [
        f"### [{title}]({abs_url})",
        f"> {p.tldr_zh or (p.abstract_zh[:120] + '…' if p.abstract_zh else '')}",
        f"- 作者：{authors}",
    ]
    if p.primary_category:
        parts.append(f"- 分类：`{p.primary_category}`")
    if badges:
        parts.append(f"- 标签：{badges}")
    if abs_url:
        parts.append(f"- 链接：{abs_url}")
    return "\n".join(parts)


def write_markdown_digest(papers: list[Paper], day: str | None = None) -> Path | None:
    """Write the Markdown digest for the given day (defaults to today UTC)."""
    if not papers:
        return None
    day = day or dt.datetime.now(dt.timezone.utc).date().isoformat()
    day_papers = [p for p in papers if p.published == day]
    if not day_papers:
        return None
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out = DIGEST_DIR / f"{day}.md"
    body = ["# redpaper · " + day, "", f"今天精选 {len(day_papers)} 篇 AI 论文：", ""]
    for p in day_papers:
        body.append(_md_paper(p))
        body.append("")
    out.write_text("\n".join(body), encoding="utf-8")
    log.info("digest written: %s", out)
    return out


def _rss_item(p: Paper) -> str:
    title = html.escape(p.title_zh or p.title)
    url = f"{SITE_URL}/post.html?id={p.id}"
    desc_text = p.tldr_zh or p.abstract_zh or p.abstract or ""
    description = html.escape(desc_text)
    pub = p.published or dt.datetime.now(dt.timezone.utc).date().isoformat()
    try:
        pub_dt = dt.datetime.fromisoformat(pub).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        pub_dt = dt.datetime.now(dt.timezone.utc)
    pub_rfc = pub_dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
    return (
        "    <item>\n"
        f"      <title>{title}</title>\n"
        f"      <link>{url}</link>\n"
        f"      <guid isPermaLink=\"true\">{url}</guid>\n"
        f"      <pubDate>{pub_rfc}</pubDate>\n"
        f"      <description>{description}</description>\n"
        "    </item>"
    )


def write_rss(papers: list[Paper], limit: int = 50) -> Path:
    items = "\n".join(_rss_item(p) for p in papers[:limit])
    body = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<rss version=\"2.0\">\n"
        "  <channel>\n"
        "    <title>redpaper</title>\n"
        f"    <link>{SITE_URL}/</link>\n"
        "    <description>每日 AI 论文小红书</description>\n"
        f"    <lastBuildDate>{dt.datetime.now(dt.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}</lastBuildDate>\n"
        f"{items}\n"
        "  </channel>\n"
        "</rss>\n"
    )
    RSS_PATH.write_text(body, encoding="utf-8")
    log.info("rss written: %s (%d items)", RSS_PATH, min(len(papers), limit))
    return RSS_PATH
