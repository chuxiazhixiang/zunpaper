"""会议官网源 —— OpenReview（CoRL / ICLR / NeurIPS 等）已接收论文。

为什么要这个：我们原来只爬 arXiv 近窗口，会议"井喷"时（CoRL/ICLR 放榜）大量被
接收的具身/机器人论文要么提交得早、在窗口外，要么作者还没在 arXiv comment 标注
venue → 都收不到。OpenReview v2 API（api2.openreview.net）**免鉴权**就能按 venueid
取某个会议的全部接收论文，这里按频道关键词过滤出机器人相关的，落成 Paper。

去重：与 arXiv 论文靠 build 的标题级去重（dedup_by_title）合并，同一篇只留一份。

体积/成本护栏：每个 venue 限 `max_per_venue` 篇；这些论文走 source="openreview"，
在 build 里**不渲染 PDF 封面**（用占位封面），避免一次性几百篇 PDF 渲染拖垮 CI。
"""
from __future__ import annotations

import datetime as dt
import logging

import requests

from ..config import Channel
from ..models import Author, Paper

log = logging.getLogger(__name__)

API = "https://api2.openreview.net/notes"
TIMEOUT = 30


def _gv(content: dict, key: str):
    """OpenReview v2 把每个字段包成 {value: ...}；这里解包。"""
    v = content.get(key)
    if isinstance(v, dict):
        return v.get("value")
    return v


def _fetch_venue(venueid: str, limit_total: int = 600, page: int = 200) -> list[dict]:
    out: list[dict] = []
    offset = 0
    while offset < limit_total:
        try:
            r = requests.get(
                API,
                params={"content.venueid": venueid, "limit": min(page, limit_total - offset), "offset": offset},
                timeout=TIMEOUT,
            )
            if r.status_code != 200:
                break
            notes = r.json().get("notes") or []
        except Exception as e:
            log.warning("openreview fetch %s @%d failed: %s", venueid, offset, e)
            break
        if not notes:
            break
        out.extend(notes)
        offset += len(notes)
        if len(notes) < page:
            break
    return out


def _matched_channels(text: str, channels: list[Channel]) -> list[str]:
    """返回命中的频道 id 列表（含 exclude 检查）。空 = 不相关。"""
    t = (text or "").lower()
    out = []
    for ch in channels:
        if ch.exclude and any(kw.lower() in t for kw in ch.exclude):
            continue
        if not ch.keywords:
            continue
        if any(kw.lower() in t for kw in ch.keywords):
            out.append(ch.id)
    return out


def _slug(forum: str) -> str:
    return f"openreview-{forum}"


def note_to_paper(n: dict, channels: list[Channel]) -> Paper | None:
    c = n.get("content") or {}
    title = (_gv(c, "title") or "").strip()
    abstract = (_gv(c, "abstract") or "").strip()
    if not title:
        return None
    kws = _gv(c, "keywords") or []
    kw_text = " ".join(kws) if isinstance(kws, list) else str(kws)
    # 只留机器人/具身相关（命中频道关键词），并直接归到命中的频道
    matched = _matched_channels(f"{title} {abstract} {kw_text}", channels)
    if not matched:
        return None
    venue = (_gv(c, "venue") or "").strip()
    authors = _gv(c, "authors") or []
    if not isinstance(authors, list):
        authors = []
    forum = n.get("forum") or n.get("id") or ""
    ts = n.get("pdate") or n.get("cdate") or 0
    pub = ""
    if ts:
        try:
            pub = dt.datetime.utcfromtimestamp(int(ts) / 1000).date().isoformat()
        except Exception:
            pub = ""
    return Paper(
        id=_slug(forum),
        source="openreview",
        title=title,
        abstract=abstract,
        authors=[Author(name=a) for a in authors[:30]],
        published=pub,
        venue=venue,
        abs_url=f"https://openreview.net/forum?id={forum}",
        pdf_url=f"https://openreview.net/pdf?id={forum}",
        channels=matched,
    )


def fetch_papers(venue_ids: list[str], channels: list[Channel],
                 max_per_venue: int = 80) -> list[Paper]:
    """按 venueid 列表取接收论文，频道关键词过滤，返回 Paper 列表。"""
    out: list[Paper] = []
    channels = list(channels or [])
    for vid in venue_ids:
        notes = _fetch_venue(vid, limit_total=max(max_per_venue * 5, 300))
        kept = 0
        for n in notes:
            if kept >= max_per_venue:
                break
            p = note_to_paper(n, channels)
            if p:
                out.append(p)
                kept += 1
        log.info("openreview[%s]: %d notes -> %d robotics papers kept", vid, len(notes), kept)
    return out
