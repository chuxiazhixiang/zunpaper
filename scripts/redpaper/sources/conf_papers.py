"""会议 / 期刊接收论文源（Semantic Scholar 按 venue + year 检索）。

补 OpenReview 覆盖不到的 CVPR / ICCV / ECCV / RA-L / ICML / ICLR / NeurIPS：S2 能按
venue + year 检索，且大多返回 arXiv id —— 我们用 arXiv slug 当 id，于是与站内已有的
arXiv 论文**天然去重 / 合并**（同一篇只补上 venue 标注，不重复成卡）。

护栏（同 OpenReview）：source="conf" 在 build 里不渲染 PDF 封面、abstract-only enrich、
跳过视频抓取；每个 venue 限 max_per_venue；整步 refresh_days 节流。
RSS 在 S2 索引不全（venue 检索常 0），仍靠 arXiv comment 路径补，不在这里抓。
"""
from __future__ import annotations

import logging
import time

import requests

from ..config import Channel
from ..models import Author, Paper

log = logging.getLogger(__name__)

API = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
TIMEOUT = 30
# 召回查询（机器人/具身向）；每个 venue 跑这几条再本地按频道关键词精筛。
_QUERIES = [
    "robot", "manipulation", "humanoid", "locomotion",
    "vision language action", "grasping", "embodied", "policy learning",
]


def _s2(query: str, venue: str, year: int) -> list[dict]:
    try:
        fields = "title,abstract,authors,externalIds,venue,year,publicationDate"
        r = requests.get(API, params={
            "query": query, "venue": venue, "year": str(year), "fields": fields,
        }, timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(5)
            r = requests.get(API, params={
                "query": query, "venue": venue, "year": str(year), "fields": fields,
            }, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return r.json().get("data") or []
    except Exception as e:
        log.warning("s2 search «%s» @%s %s failed: %s", query, venue, year, e)
        return []


def _matched_channels(text: str, channels: list[Channel]) -> list[str]:
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


def _slug(arxiv_id: str) -> str:
    return f"arxiv-{arxiv_id.replace('.', '-')}"


def fetch_papers(venue_specs: list[dict], channels: list[Channel],
                 max_per_venue: int = 40) -> list[Paper]:
    """venue_specs: [{query, short, year}]。query=S2 venue 检索名，short=展示简称。"""
    out: dict[str, Paper] = {}
    channels = list(channels or [])
    for spec in venue_specs:
        vq, short, year = spec.get("query"), spec.get("short"), spec.get("year")
        if not (vq and short and year):
            continue
        seen: set[str] = set()
        kept = 0
        for q in _QUERIES:
            if kept >= max_per_venue:
                break
            for p in _s2(q, vq, year):
                pid = p.get("paperId")
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                title = (p.get("title") or "").strip()
                if not title:
                    continue
                abstract = (p.get("abstract") or "").strip()
                matched = _matched_channels(f"{title} {abstract}", channels)
                if not matched:
                    continue
                ext = p.get("externalIds") or {}
                aid = ext.get("ArXiv")
                if aid:
                    idd = _slug(aid)
                    abs_url = f"https://arxiv.org/abs/{aid}"
                    pdf = f"https://arxiv.org/pdf/{aid}"
                else:
                    idd = f"s2-{pid}"
                    abs_url = f"https://www.semanticscholar.org/paper/{pid}"
                    pdf = ""
                if idd in out:
                    continue
                authors = [Author(name=a.get("name", "")) for a in (p.get("authors") or [])[:30] if a.get("name")]
                # 发布日期：S2 publicationDate（YYYY-MM-DD）优先，否则用年份首日（让它按
                # 年份归档、不挤进"今天"）。merge 到已有 arxiv 论文时会继承更早的真实日期。
                pub = (p.get("publicationDate") or "").strip() or f"{year}-01-01"
                out[idd] = Paper(
                    id=idd, source="conf", title=title, abstract=abstract,
                    authors=authors, arxiv_id=(aid or ""), pdf_url=pdf, abs_url=abs_url,
                    published=pub, venue=f"{short} {year}", channels=matched,
                )
                kept += 1
                if kept >= max_per_venue:
                    break
            time.sleep(2.0)  # S2 未鉴权限额，留余量
        log.info("conf[%s %s]: %d robotics papers kept", short, year, kept)
    return list(out.values())
