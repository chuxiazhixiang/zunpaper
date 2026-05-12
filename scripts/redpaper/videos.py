"""Demo video extraction (P0).

给每篇 paper 找 demo 视频——人形 / 机器人圈极度依赖 demo 是否炸裂。Scholar Inbox
没有这块；这里把它做透就是核心 wedge。

策略（按命中率从高到低）：
  1. 已抓到的 project page URL（`related_links` 里 source == 'project'）
     → 直接拉 HTML，正则抠 <iframe src="…youtube…"> 或 <iframe src="…bilibili…">。
  2. 摘要文本里出现的 URL（很多 arXiv 摘要里就直接写 "Project page: https://..."）
     → 同上。
  3. 摘要里直接出现的 youtu.be / youtube.com / bilibili.com 链接 → 直接收下。

返回的 `demo_videos` 每项形如：
    {
      "kind":      "youtube" | "bilibili" | "mp4",
      "url":       原始 URL,
      "embed_url": 可放 <iframe> 的 embed 形式,
      "title":     抓到 <title> 的话填上，没抓到就空字符串,
      "source":    "project_page" | "abstract"  （来源标注）
    }

缓存：`data/video_cache.json`，key=paper.id，value={videos: [...], project_page: "..."}。

设计原则：
  - 完全离线 / 纯文本正则，不引入 selenium / playwright。
  - 单 paper 抓取 ≤ 1 个 HTTP 请求（拿 1 个项目页）。
  - 抓不到就静默跳过，不让一篇连不上的项目页阻塞整个 pipeline。
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ----- 正则：URL / 视频识别 -----------------------------------------------------
# 摘要 / HTML 里通用 URL 抓取。允许带常见结尾标点（句号、逗号、引号）。
_URL_RE = re.compile(r"https?://[^\s<>\"'\)]+", re.IGNORECASE)

# YouTube：标准 watch?v= / youtu.be / embed/。捕获 11 位 video id。
_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
# Bilibili：BV 号 或 av 号。
_BILI_BV_RE = re.compile(r"bilibili\.com/video/(BV[A-Za-z0-9]{10})", re.IGNORECASE)
_BILI_AV_RE = re.compile(r"bilibili\.com/video/av(\d+)", re.IGNORECASE)
_BILI_EMBED_RE = re.compile(r"player\.bilibili\.com/player\.html\?[^\"'<>]*bvid=(BV[A-Za-z0-9]{10})", re.IGNORECASE)

# self-hosted mp4（项目页常见）
_MP4_RE = re.compile(r"https?://[^\s<>\"']+?\.mp4(?:\?[^\s<>\"']*)?", re.IGNORECASE)


@dataclass
class VideoCache:
    """`data/video_cache.json` 包装。"""
    path: Path
    entries: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text("utf-8"))
            except Exception:
                self.entries = {}

    def get(self, pid: str) -> dict | None:
        return self.entries.get(pid)

    def put(self, pid: str, value: dict) -> None:
        self.entries[pid] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2, sort_keys=True),
            "utf-8",
        )


# ----- 单条 URL → video 描述 ---------------------------------------------------
def _from_youtube_id(vid: str, source: str, title: str = "") -> dict:
    return {
        "kind": "youtube",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "embed_url": f"https://www.youtube.com/embed/{vid}",
        "title": title,
        "source": source,
    }


def _from_bilibili_bv(bv: str, source: str, title: str = "") -> dict:
    return {
        "kind": "bilibili",
        "url": f"https://www.bilibili.com/video/{bv}",
        # high_quality=1 拿高清，autoplay=0 防止打开就响。
        "embed_url": f"https://player.bilibili.com/player.html?bvid={bv}&high_quality=1&autoplay=0",
        "title": title,
        "source": source,
    }


def _from_mp4(url: str, source: str, title: str = "") -> dict:
    return {
        "kind": "mp4",
        "url": url,
        "embed_url": url,
        "title": title,
        "source": source,
    }


# ----- 文本 / HTML 扫描 --------------------------------------------------------
def _scan_text_for_videos(text: str, source: str) -> list[dict]:
    """从任意文本里抠 youtube / bilibili 视频。"""
    if not text:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    for m in _YT_RE.finditer(text):
        vid = m.group(1)
        if vid in seen:
            continue
        seen.add(vid)
        out.append(_from_youtube_id(vid, source))

    for m in _BILI_BV_RE.finditer(text):
        bv = m.group(1)
        if bv in seen:
            continue
        seen.add(bv)
        out.append(_from_bilibili_bv(bv, source))

    for m in _BILI_EMBED_RE.finditer(text):
        bv = m.group(1)
        if bv in seen:
            continue
        seen.add(bv)
        out.append(_from_bilibili_bv(bv, source))

    return out


def _scan_html_for_html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    raw = re.sub(r"\s+", " ", m.group(1)).strip()
    # 去 trailing "| GitHub Pages" / "- Project" 之类装饰
    return raw[:120]


def fetch_project_page(url: str, *, timeout: float = 8.0) -> str:
    """拉项目主页 HTML。抓不到返回空字符串。"""
    try:
        # 一些项目页有 hash fragment（#demo），requests 会自动剥掉，不影响。
        r = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "redpaper-video-scraper/1.0 (+https://github.com/)",
                "Accept-Language": "en;q=0.9,zh;q=0.8",
            },
            allow_redirects=True,
        )
        if r.status_code >= 400:
            return ""
        # 项目页通常 <50KB，但保险起见截 800KB。
        return r.text[:800_000]
    except Exception:
        return ""


# ----- 项目页 URL 候选挑选 -----------------------------------------------------
def _candidate_project_urls(paper) -> list[str]:
    """按优先级返回项目主页候选 URL 列表。"""
    urls: list[str] = []

    # 1. related_links 里显式标记 source == 'project'
    for l in getattr(paper, "related_links", None) or []:
        if l.get("source") == "project" and l.get("url"):
            urls.append(l["url"])

    # 2. abstract 文本里出现的 URL —— 过滤掉 arxiv / openreview 这种不是项目页的。
    abstract = getattr(paper, "abstract", "") or ""
    for u in _URL_RE.findall(abstract):
        # 把句号 / 反引号 / markdown 引号清掉
        u = u.rstrip(".,)\"'`")
        if not u:
            continue
        host = urllib.parse.urlsplit(u).netloc.lower()
        if not host:
            continue
        # arxiv / 论文托管 / OpenReview / PubMed 这种不是项目页
        if any(bad in host for bad in (
            "arxiv.org", "openreview.net", "papers.nips", "papers.neurips",
            "proceedings.mlr", "doi.org", "ncbi.nlm.nih.gov", "ieeexplore",
            "dl.acm.org", "pubmed", "semanticscholar.org",
        )):
            continue
        if u not in urls:
            urls.append(u)

    return urls[:3]  # 最多看 3 个候选，避免一篇 paper 烧太多请求


# ----- 主接口 ------------------------------------------------------------------
def extract_videos_for_paper(paper) -> dict:
    """给一篇 paper 抓 demo 视频。返回 cache entry dict。"""
    videos: list[dict] = []
    seen_keys: set[str] = set()

    def _add(v: dict) -> None:
        # 用 (kind, url) 去重
        k = (v["kind"], v["url"])
        if k in seen_keys:
            return
        seen_keys.add(k)
        videos.append(v)

    # Step 1: 摘要里直接出现的 youtube / bilibili 链接
    for v in _scan_text_for_videos(
        getattr(paper, "abstract", "") or "",
        source="abstract",
    ):
        _add(v)

    # Step 2: 拉项目页 HTML 找 iframe / 视频
    candidates = _candidate_project_urls(paper)
    project_page = ""
    for url in candidates:
        html = fetch_project_page(url)
        if not html:
            continue
        # 整页扫一遍
        title = _scan_html_for_html_title(html)
        before = len(videos)
        for v in _scan_text_for_videos(html, source="project_page"):
            v["title"] = v.get("title") or title
            _add(v)
        # 抓 mp4：项目页常见自托管 highlight reel
        for m in _MP4_RE.finditer(html):
            mp4_url = m.group(0)
            if "demo" not in mp4_url.lower() and "video" not in mp4_url.lower() \
               and "reel" not in mp4_url.lower() and "teaser" not in mp4_url.lower():
                # 跳过看起来是图标 / 装饰资源的（保险起见）
                continue
            _add(_from_mp4(mp4_url, "project_page", title))
        if len(videos) > before:
            # 找到就停，不再继续撞别的 URL（避免命中弱信号干扰）
            project_page = url
            break
        # 没找到但项目页拉到了，记一下，方便后续 fallback
        project_page = project_page or url

    return {
        "videos": videos[:4],          # 最多保 4 个，前端只展示 3 个
        "project_page": project_page,  # 留个手柄，将来 P1++ 还能用
    }


def enrich_paper_videos(paper, cache: VideoCache, *, force: bool = False) -> list[dict]:
    """带缓存的批接口：从 cache 取，缺则现抓。返回 video list。"""
    cached = None if force else cache.get(paper.id)
    if cached is not None:
        videos = cached.get("videos") or []
        return list(videos)
    result = extract_videos_for_paper(paper)
    cache.put(paper.id, result)
    # 轻微 sleep 防止把项目主页打爆
    time.sleep(0.4)
    return result["videos"]


__all__ = [
    "VideoCache",
    "extract_videos_for_paper",
    "enrich_paper_videos",
    "fetch_project_page",
]
