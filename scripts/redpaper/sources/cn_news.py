"""Chinese AI media: 量子位 / 机器之心 / 新智元.

Two roles:
1. Enrich existing arXiv papers — scan recent articles for arXiv-id mentions
   and attach the article as a "related link" on that paper (already done by
   build_arxiv_index).
2. Stand-alone news cards (added 2026-05) — articles without an arXiv mention
   are converted to Paper objects and shown as cards on the homepage, scoped
   to channels whose keywords appear in the title/description. See
   `fetch_news_papers()`.

The HTML/RSS endpoints are public, so no key needed. They are best-effort —
selectors will rot, and a failure of one source must not break the pipeline.

Known RSS health as of 2026-05:
- qbitai /feed                       OK (~10 latest items, mixed topics)
- qbitai /category/robot/feed        empty
- jiqizhixin /rss                    parses OK but body is empty
- aiera.com.cn /feed (新智元)         timeout / down
So in practice only 量子位 is producing live news cards right now.
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

QBITAI_LIST = "https://www.qbitai.com/feed"  # /category/ai/feed 已空，主 feed 还活
# 2026-05: 量子位 title 不再用 CDATA 包裹，所以这里用更宽松的解析路径，
# 一次抓出整个 <item> 块，再分别提取字段。
QBITAI_ITEM_BLOCK_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)


def _field(block: str, name: str) -> str:
    """Extract `<name>...</name>` from an RSS item block, stripping optional
    CDATA wrappers. Empty string if absent."""
    m = re.search(rf"<{name}[^>]*>(.*?)</{name}>", block, re.DOTALL)
    if not m:
        return ""
    raw = m.group(1).strip()
    cd = re.match(r"<!\[CDATA\[(.*?)\]\]>", raw, re.DOTALL)
    return (cd.group(1) if cd else raw).strip()


def fetch_qbitai(limit: int = 20) -> list[NewsArticle]:
    """Old arxiv-id-extraction path — kept for `build_arxiv_index`."""
    feed = _safe_get(QBITAI_LIST)
    triples: list[tuple[str, str, str]] = []
    for block in QBITAI_ITEM_BLOCK_RE.findall(feed):
        title = _field(block, "title")
        url = _field(block, "link")
        pubdate = _field(block, "pubDate")
        if title and url:
            triples.append((title, url, pubdate[:16]))
    return _scan_articles(triples, "qbitai", "量子位", limit)


def _parse_qbitai_items() -> list[dict]:
    """Lightweight parse — no per-article fetch. Used by news-card path."""
    feed = _safe_get(QBITAI_LIST)
    out = []
    for block in QBITAI_ITEM_BLOCK_RE.findall(feed):
        title = _field(block, "title")
        url = _field(block, "link")
        if not title or not url:
            continue
        pubdate = _field(block, "pubDate")
        desc = re.sub(r"<[^>]+>", " ", _field(block, "description")).strip()
        out.append({
            "title": title,
            "url": url,
            "pubdate": pubdate,
            "desc": desc,
        })
    return out


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


# ----------------------------------------------------------------------
# News-as-card path: produce Paper objects from media articles so they show
# up on the homepage alongside arXiv papers.
# ----------------------------------------------------------------------
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
import datetime as _dt
import hashlib

from ..models import Paper
from .. import config as _cfg


def _slug(source: str, url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"{source}-{h}"


def _parse_rfc822(s: str) -> str:
    """RSS pubDate (RFC 822) -> 'YYYY-MM-DD'. Empty string on parse failure."""
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return ""


# ----- Article image extraction -----------------------------------------
# 公众号文章里基本上是「头图 + 正文穿插」结构，第一张正文图（不算 logo /
# nav / 二维码）往往就是论文 pipeline 或者标题图。我们直接 grep 最大概率
# 的几个 selector，命中就用，命中不到留空让前端走 CSS 兜底。

_IMG_RE = re.compile(
    r'<img[^>]+(?:data-src|data-original|src)\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
# 各家正文容器：<article>、.post-content、.article（量子位）、#js_content（公众号）等
_ARTICLE_BLOCK_RE = re.compile(
    r'<article[^>]*>(.*?)</article>'
    r'|<div[^>]+(?:class|id)="[^"]*\b(?:post[-_]content|article[-_]content|article|js_content|main[-_]content|content[-_]body|article-body|entry-content|single-content|post-body)\b[^"]*"[^>]*>(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)

# 站内静态资源 / logo / 二维码 / 默认头像 / 占位图 —— 一律不要
_IMG_SKIP_RE = re.compile(
    r'(?:logo|qrcode|qr_code|avatar|spinner|emoji|widget|gravatar|footer|advert|'
    r'/themes?/|imagesnew/|/head\.|nologin|sprite|favicon|placeholder|blank\.|1x1\.)',
    re.IGNORECASE,
)

# 网站头像/默认头图也常用 1x1 占位 → URL 里有 transparent / blank 字样
def _looks_like_real_image(src: str) -> bool:
    if not src:
        return False
    if _IMG_SKIP_RE.search(src):
        return False
    # data URI / base64 不要
    if src.startswith("data:"):
        return False
    return True


def _img_score(src: str, base_host: str) -> int:
    """Heuristic score: higher = more likely the article hero image.

    Signals: + on dedicated image CDNs / uploads paths
             + on JPG/PNG with year/month folder
             - on site-template paths
             - on tiny WP-generated thumbnail variants (filename has -WxH)
             - on filenames matching size patterns like 100x100"""
    s = src.lower()
    score = 0
    if "wp-content/uploads/" in s or "/uploads/20" in s:
        score += 5  # 文章自上传图，强信号
    if any(cdn in s for cdn in ("i.qbitai.com", "img.36krcdn.com", "p1.itc.cn",
                                  "img.jiqizhixin.com", "static.leiphone.com",
                                  "media-library", "wp-content/uploads")):
        score += 3
    # 网站主域 + theme 静态资源 → 大概率是 logo / banner
    if "/themes/" in s or "imagesnew" in s:
        score -= 10
    # WP 自动缩略图：filename-WxH 后缀（一般是 banner / 头像缩略）
    m = re.search(r"-(\d+)x(\d+)\.(?:jpe?g|png|webp)", s)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if max(w, h) < 400:
            score -= 4  # 小缩略图，大概率不是首图
        elif max(w, h) < 700:
            score -= 1
    # 同时包含 RoboReport / report-header 之类的 banner 文件名
    if any(b in s for b in ("roboreport-", "header-", "banner-", "logo-")):
        score -= 5
    return score


def _extract_first_image(html: str, base_url: str) -> str:
    """Return the absolute URL of the most-likely article hero image, or ''.

    Strategy: scan ALL <img> on the page (regex 是出了名的对嵌套 div 不友
    好，硬要圈正文块经常落空)；先用黑名单剔掉明显是 logo / 二维码 / 主
    题资源的图片；再按启发式打分挑出最像首图的那张。
    """
    if not html:
        return ""
    base_host = urlparse(base_url).netloc
    scored: list[tuple[int, int, str]] = []  # (score, index, src)
    seen: set[str] = set()
    for idx, src in enumerate(_IMG_RE.findall(html)):
        if src in seen:
            continue
        seen.add(src)
        if not _looks_like_real_image(src):
            continue
        scored.append((_img_score(src, base_host), idx, src))
    if not scored:
        return ""
    # 高分优先；同分时取顺序靠前的那张（一般是首图）
    scored.sort(key=lambda t: (-t[0], t[1]))
    src = scored[0][2]
    if src.startswith("//"):
        src = "https:" + src
    elif src.startswith("/"):
        parsed = urlparse(base_url)
        src = f"{parsed.scheme}://{parsed.netloc}{src}"
    elif not src.startswith(("http://", "https://")):
        src = urljoin(base_url, src)
    return src


def _download_image(url: str, out_path: Path) -> bool:
    """Best-effort: download `url` and save as JPEG at `out_path`.
    Resizes oversize images down to 1200px on the long edge to keep covers
    light. Returns True on success."""
    try:
        r = requests.get(
            url,
            timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
            stream=True,
        )
        r.raise_for_status()
        body = r.content
    except Exception as e:
        log.debug("image fetch %s failed: %s", url, e)
        return False
    try:
        from io import BytesIO
        from PIL import Image  # already a dep via render.py
        im = Image.open(BytesIO(body)).convert("RGB")
        # downscale only
        max_side = 1200
        w, h = im.size
        if max(w, h) > max_side:
            scale = max_side / max(w, h)
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        im.save(out_path, "JPEG", quality=85, optimize=True)
        return True
    except Exception as e:
        log.debug("image save %s failed: %s", url, e)
        return False


def _to_site_rel(p: Path) -> str:
    return str(p.relative_to(_cfg.SITE_DIR))


def _channels_for(text: str, channels) -> list[str]:
    """Run the user's channel keyword filter against a text blob. Returns
    every channel id whose keyword list matches (and whose exclude list
    doesn't match). Empty list = topic doesn't belong on this site."""
    low = text.lower()
    matched: list[str] = []
    for ch in channels:
        if ch.exclude and any(kw.lower() in low for kw in ch.exclude):
            continue
        if not ch.keywords:
            continue
        if any(kw.lower() in low for kw in ch.keywords):
            matched.append(ch.id)
    return matched


def _has_chinese(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


# ----------------------------------------------------------------------
# Multi-source parsers (RSS + qbitai HTML deep crawl)
# ----------------------------------------------------------------------
# Each parser returns: list[dict(title, url, pubdate, desc, lang)]
# - pubdate: best-effort string (RFC822 / ISO)
# - lang: 'zh' or 'en' — controls whether we run translate.translate()
# ----------------------------------------------------------------------

_ITEM_BLOCK_RE = re.compile(r"<item>(.*?)</item>", re.DOTALL)
_ENTRY_BLOCK_RE = re.compile(r"<entry[^>]*>(.*?)</entry>", re.DOTALL)


def _rss_items(feed: str) -> list[str]:
    return _ITEM_BLOCK_RE.findall(feed)


def _atom_entries(feed: str) -> list[str]:
    return _ENTRY_BLOCK_RE.findall(feed)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s).strip()


def _generic_rss(url: str, lang: str) -> list[dict]:
    feed = _safe_get(url)
    if not feed:
        return []
    out = []
    blocks = _rss_items(feed) or _atom_entries(feed)
    for block in blocks:
        title = _field(block, "title")
        # <link> 在 RSS 里是文本，但 Atom 里是 <link href="..."/>
        link = _field(block, "link")
        if not link:
            m = re.search(r'<link[^>]+href="([^"]+)"', block)
            if m:
                link = m.group(1)
        pubdate = _field(block, "pubDate") or _field(block, "published") or _field(block, "updated")
        desc = _field(block, "description") or _field(block, "summary") or _field(block, "content")
        desc = _strip_tags(desc)[:400]
        if not title or not link:
            continue
        out.append({"title": title, "url": link, "pubdate": pubdate, "desc": desc, "lang": lang})
    return out


# ----- 量子位 -----------------------------------------------------------

def _parse_qbitai_rss() -> list[dict]:
    feed = _safe_get(QBITAI_LIST)
    out = []
    for block in QBITAI_ITEM_BLOCK_RE.findall(feed):
        title = _field(block, "title")
        url = _field(block, "link")
        if not title or not url:
            continue
        pubdate = _field(block, "pubDate")
        desc = _strip_tags(_field(block, "description"))
        out.append({"title": title, "url": url, "pubdate": pubdate, "desc": desc, "lang": "zh"})
    return out


# 标题锚点（h2/h3/h4 包裹的 `<a href="...">title</a>`）
_QBITAI_TITLE_ANCHOR_RE = re.compile(
    r'<h[2-4][^>]*>\s*<a[^>]+href="(https://www\.qbitai\.com/(\d{4})/(\d{2})/\d+\.html)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
# 时间标签：qbitai archive 把发布时刻塞在 `<span class="time">` 里，
# 格式有三种：绝对日期 `2026-06-05` / 相对天 `昨天 11:17` `前天 20:43` /
# 相对小时 `2小时前` `22小时前`。
_QBITAI_TIME_SPAN_RE = re.compile(r'<span class="time">\s*([^<]+?)\s*</span>')

_QBITAI_ABS_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_QBITAI_HOURS_AGO_RE = re.compile(r"^(\d+)\s*小时前")
_QBITAI_DAYS_AGO_RE = re.compile(r"^(\d+)\s*天前")


def _qbitai_time_to_iso(text: str, today: _dt.date) -> str:
    """Convert qbitai 的 `<span class="time">` 文本到 ISO 日期。

    覆盖以下几种 qbitai 实际能渲染的格式：
      - `2026-06-05`           → 直接拿
      - `2小时前` / `22小时前` → 今天（小时窗口同日）
      - `昨天 11:17`           → 今天 - 1 天
      - `前天 20:43`           → 今天 - 2 天
      - `3天前`                → 今天 - N 天
    解析不出来就返回空串，让上游兜底（保持空 pubdate，前端不会乱排序）。
    """
    text = (text or "").strip()
    if not text:
        return ""
    if m := _QBITAI_ABS_DATE_RE.match(text):
        return m.group(1)
    if _QBITAI_HOURS_AGO_RE.match(text):
        return today.isoformat()
    if text.startswith("昨天"):
        return (today - _dt.timedelta(days=1)).isoformat()
    if text.startswith("前天"):
        return (today - _dt.timedelta(days=2)).isoformat()
    if m := _QBITAI_DAYS_AGO_RE.match(text):
        return (today - _dt.timedelta(days=int(m.group(1)))).isoformat()
    return ""


def _iso_to_rfc822(iso: str) -> str:
    """ISO 日期串转 RFC822（与 RSS pubDate 对齐，方便下游 _parse_rfc822 复用）。"""
    try:
        d = _dt.date.fromisoformat(iso)
        return d.strftime("%a, %d %b %Y 00:00:00 +0800")
    except Exception:
        return ""


def _parse_qbitai_archive(pages: int = 8) -> list[dict]:
    """Walk qbitai HTML pagination (/page/2 … /page/N) for older posts.
    The RSS only carries ~10 latest; this gets us back ~25 posts per page
    so ~200 candidates over 8 pages = roughly 25-40 days back.

    每篇文章在 archive 页里同时有 `<h4><a>title</a></h4>` 和紧随其后的
    `<span class="time">...</span>`。我们按位置把这俩一一配对：标题锚点
    之后第一个未消耗过的 time 标签，就是该文章的发布时间。

    历史教训：之前只从 URL 取年月、当月默认 15 号 → 当月所有 archive 文章
    被打上同一天的「未来夹紧」戳，导致 6/5–6/8 文章全显示成今天的日期、
    和真实发布时间错位（也会被去重/排序逻辑误判为"陈旧"）。
    """
    seen: dict[str, dict] = {}
    today = _dt.date.today()
    for p in range(1, pages + 1):
        url = f"https://www.qbitai.com/page/{p}"
        html = _safe_get(url)
        if not html:
            continue
        # 收集页面里所有 time 标签的 (位置, 文本)，留待按位置匹配。
        time_spans: list[tuple[int, str]] = [
            (m.start(), m.group(1)) for m in _QBITAI_TIME_SPAN_RE.finditer(html)
        ]
        cursor = 0  # 已经被消耗到的 time span 下标
        # 用 finditer 拿到每个标题锚点的位置，按出现顺序处理。
        for m in _QBITAI_TITLE_ANCHOR_RE.finditer(html):
            purl = m.group(1)
            inner = _strip_tags(m.group(4))
            inner = re.sub(r"\s+", " ", inner).strip()
            if len(inner) < 3:
                continue
            if purl in seen:
                continue
            # 找到位置在该标题之后、还没被任何标题消耗掉的最近一个 time span
            iso = ""
            while cursor < len(time_spans) and time_spans[cursor][0] < m.end():
                cursor += 1
            if cursor < len(time_spans):
                iso = _qbitai_time_to_iso(time_spans[cursor][1], today)
                cursor += 1
            seen[purl] = {
                "title": inner,
                "url": purl,
                "pubdate": _iso_to_rfc822(iso),
                "desc": "",
                "lang": "zh",
            }
        time.sleep(0.4)
    return list(seen.values())


def _parse_qbitai() -> list[dict]:
    """Combined: RSS (recent) + HTML archive (older). Dedup by URL."""
    by_url: dict[str, dict] = {}
    for it in _parse_qbitai_rss():
        by_url[it["url"]] = it
    for it in _parse_qbitai_archive(pages=8):
        if it["url"] not in by_url:
            by_url[it["url"]] = it
    return list(by_url.values())


# ----- jintiankansha 公众号镜像 -----------------------------------------
# jintiankansha 是一个 WeChat 公众号 -> 文章列表镜像站，每个公众号都有一个
# `/column/{ID}` 页面。前后端都是 SSR HTML，没有 RSS endpoint，只能 grep。
# 列表页 ~20 条最新文章，详情页有正文 + 首图（mmbiz / sinaimg 域名）。

# 列表页文章 URL 形如：http://www.jintiankansha.me/t/{10-char-hash}
_JTKS_ITEM_RE = re.compile(
    r'href="(http://www\.jintiankansha\.me/t/[A-Za-z0-9]+)"[^>]*>([^<]+)</a>',
    re.DOTALL,
)
# 日期是中文相对时间："3 天前" / "1 周前" / "5 小时前"。我们能把它换算成 ISO。
_JTKS_RELDATE_RE = re.compile(r'(\d+)\s*(小时|天|周|月)\s*前')


def _jtks_reldate_to_iso(text: str, anchor: _dt.date | None = None) -> str:
    m = _JTKS_RELDATE_RE.search(text or "")
    if not m:
        return ""
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "小时": _dt.timedelta(hours=n),
        "天":   _dt.timedelta(days=n),
        "周":   _dt.timedelta(weeks=n),
        "月":   _dt.timedelta(days=n * 30),  # 近似
    }.get(unit, _dt.timedelta())
    today = anchor or _dt.date.today()
    when = today - _dt.timedelta(days=delta.days)
    return when.isoformat()


def _parse_jintiankansha(column_id: str, name: str) -> list[dict]:
    """Scrape a 今天看啥 column. Pages are SSR HTML; we 30-item per fetch."""
    url = f"http://www.jintiankansha.me/column/{column_id}"
    html = _safe_get(url)
    if not html:
        return []
    out: list[dict] = []
    seen_urls: set[str] = set()
    # 整张页面按 <tr> / <li> 分块再 grep 太脆弱；直接收所有 /t/xxx
    # 链接对，再把附近的相对日期对应回去。
    items_raw: list[tuple[str, str, int]] = []  # (url, title, position)
    for m in _JTKS_ITEM_RE.finditer(html):
        purl = m.group(1)
        title = re.sub(r"\s+", " ", m.group(2)).strip()
        if not title or purl in seen_urls:
            continue
        seen_urls.add(purl)
        items_raw.append((purl, title, m.start()))

    # 把页面上所有 "X 天前" 的位置取出来，按最近的一个匹配到上面的 link
    reldate_positions: list[tuple[int, str]] = [
        (m.start(), m.group(0)) for m in _JTKS_RELDATE_RE.finditer(html)
    ]

    for purl, title, pos in items_raw:
        # 找到该 link 附近 (前后 1500 字符内) 最近的相对日期串
        nearest = ""
        for rpos, rtext in reldate_positions:
            if abs(rpos - pos) <= 1500:
                nearest = rtext
                break
        iso = _jtks_reldate_to_iso(nearest)
        # 用 RFC822 风格存 pubdate，让下游 _parse_rfc822 一致处理
        pubdate = ""
        if iso:
            try:
                d = _dt.date.fromisoformat(iso)
                pubdate = d.strftime("%a, %d %b %Y 00:00:00 +0800")
            except Exception:
                pubdate = ""
        out.append({
            "title": title,
            "url": purl,
            "pubdate": pubdate,
            "desc": "",       # 列表页没摘要；详情页里会被 cover-extract 拉一次
            "lang": "zh",
        })
    return out


# 公众号 → 今天看啥 column id
JTKS_TECHDAILY = "OyGGse6DLa"   # 具身智能之心 / 具身智能之心TechDaily
JTKS_SHENLAN   = "ZF2cB1xCBa"   # 深蓝AI / 深蓝具身智能（已合并到主号，老 column 仍有近半年文章）


def _parse_embodied_techdaily() -> list[dict]:
    return _parse_jintiankansha(JTKS_TECHDAILY, "具身智能之心")


def _parse_shenlan() -> list[dict]:
    return _parse_jintiankansha(JTKS_SHENLAN, "深蓝具身智能")


def _build_fetchers(enabled: dict[str, bool]) -> list[tuple[str, str, callable]]:
    """Return (source_id, display_name, parser) for each enabled source.

    用户精选的高质量公众号 / 镜像站。删掉了之前的雷峰网 / 36kr / IEEE Spectrum /
    Robohub / TheRobotReport / TechCrunch / Synced Review —— 它们大多是行业广告
    / 融资八卦 / Disrupt 会议宣传，学术含量低。
    """
    chain: list[tuple[str, str, callable]] = []
    if enabled.get("qbitai"):
        chain.append(("qbitai", "量子位", _parse_qbitai))
    if enabled.get("embodied_techdaily"):
        chain.append(("embodied_techdaily", "具身智能之心", _parse_embodied_techdaily))
    if enabled.get("shenlan_embodied"):
        chain.append(("shenlan_embodied", "深蓝具身智能", _parse_shenlan))
    return chain


def _max_age_days(s: str, days: int) -> bool:
    """Returns True if the pubdate is within `days` of today, OR we cannot
    parse the date (then we keep the item to be safe — better some noise
    than dropping good content because of a date-format edge case)."""
    if not s or days <= 0:
        return True
    iso = _parse_rfc822(s)
    if not iso:
        return True
    try:
        d = _dt.date.fromisoformat(iso)
    except Exception:
        return True
    return (_dt.date.today() - d).days <= days


def fetch_news_papers(enabled: dict[str, bool], channels,
                       limit_per_source: int = 200,
                       fetch_covers: bool = True,
                       max_age_days: int = 0,
                       translate_en: bool = False) -> list[Paper]:
    """Produce stand-alone news Paper cards from enabled media sources.

    Args:
        enabled: dict of {source_id: bool}
        channels: list of Channel — used for keyword filtering
        limit_per_source: max items per source after parsing (before filter)
        fetch_covers: download first article image as cover
        max_age_days: drop items older than N days (0 = no age filter)
        translate_en: 已废弃 — 英文条目交给主 pipeline 的 translate_with_retry 处理，
                      这里只设置 source 信息，避免被认成"已翻译过"。Chinese
                      条目则直接复用原文（中文翻成中文徒劳无功）。
    """
    out: list[Paper] = []
    fetchers = _build_fetchers(enabled)
    covers_dir = _cfg.COVER_DIR

    for source, source_name, parser in fetchers:
        try:
            items = parser()[:limit_per_source]
        except Exception as e:
            log.warning("%s news fetch failed: %s", source, e)
            continue

        kept = 0
        skipped_age = 0
        skipped_topic = 0
        for it in items:
            title = it.get("title", "").strip()
            url = it.get("url", "").strip()
            desc = it.get("desc", "").strip()
            lang = it.get("lang", "zh")
            pubdate_raw = it.get("pubdate", "")
            if not title or not url:
                continue

            if max_age_days and not _max_age_days(pubdate_raw, max_age_days):
                skipped_age += 1
                continue

            channel_ids = _channels_for(title + "\n" + desc, channels)
            if not channel_ids:
                skipped_topic += 1
                continue

            pub = _parse_rfc822(pubdate_raw)
            slug = _slug(source, url)

            # 中文条目：跳过 LLM 翻译，title_zh / abstract_zh 直接复用原文。
            # 英文条目：保持 zh 字段为空，让主 pipeline 的 translate_with_retry
            #          后续帮忙翻成中文（自带重试/多 backend fallback）。
            if lang == "zh":
                title_zh = title
                abstract_zh = desc
                tldr_zh = (desc or title)[:80]
                cover_zh = tldr_zh
            else:
                title_zh = ""
                abstract_zh = ""
                tldr_zh = ""
                cover_zh = ""

            cover_rel = ""
            if fetch_covers:
                cover_path = covers_dir / f"{slug}.jpg"
                if cover_path.exists():
                    cover_rel = _to_site_rel(cover_path)
                else:
                    html = _safe_get(url)
                    img_url = _extract_first_image(html, url) if html else ""
                    if img_url and _download_image(img_url, cover_path):
                        cover_rel = _to_site_rel(cover_path)
                        log.info("news cover saved: %s ← %s", slug, img_url[:80])

            p = Paper(
                id=slug,
                source=source,
                title=title,
                abstract=desc or title,
                title_zh=title_zh,
                abstract_zh=abstract_zh,
                tldr_zh=tldr_zh,
                cover_zh=cover_zh,
                authors=[],
                primary_category="",
                categories=[],
                published=pub,
                updated=pub,
                arxiv_id="",
                pdf_url="",
                abs_url=url,
                cover_image=cover_rel,
                channels=channel_ids,
                source_tags=[source],
            )
            out.append(p)
            kept += 1

        log.info("news[%s]: %d kept / %d total (skipped: %d off-topic, %d too-old)",
                 source, kept, len(items), skipped_topic, skipped_age)

    log.info("fetched %d news papers total", len(out))
    return out
