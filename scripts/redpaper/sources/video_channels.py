"""Video channel sources (P5): YouTube + Bilibili.

把"科研内容生态"从纯论文扩到 demo 视频。Boston Dynamics / Figure / 1X / Unitree
这类厂商不发 paper，但 demo 视频本身就是 SOTA 的"信号"；中国具身机器人圈
B 站账号（量子位 / 智元 / 宇树）也是关键风向标。

实现：
  - YouTube：直接吃官方 RSS（https://www.youtube.com/feeds/videos.xml?channel_id=UCxxx）。
    完全无鉴权、稳定、最近 15 条。
  - Bilibili：走 rsshub.app/bilibili/user/video/{uid}。第三方实例，限速但够用；
    抓不到就跳过这条频道，不会拖垮整个 pipeline。

每条视频转成一个 Paper 卡，跟 cn_news 一样从 sources.yaml 控开关。Paper 卡的
`demo_videos` 字段直接填这条视频本身，前端 cover carousel 会嵌入。

返回的 Paper 卡都打上 channel="news"（沿用 news 的渲染路径，省得新加 ChannelKind），
但 source="video_youtube" 或 "video_bilibili" 用于区分来源 + 角标。
"""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Callable

import requests

from ..models import Paper

log = logging.getLogger(__name__)

# B 站 anti-bot 越来越严，请求里要让自己看起来像浏览器。Referer 也得带上对应
# 用户空间页，否则会触发 -352 风控错误。
UA_BROWSER = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
UA = "Mozilla/5.0 (compatible; redpaper/0.1; video-channel-fetcher)"
TIMEOUT = 20

YT_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
BILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILI_SPACE_URL = "https://api.bilibili.com/x/space/wbi/arc/search"

# Atom 命名空间（YouTube feed 用 atom + 自定义 media:）
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


@dataclass
class VideoItem:
    platform: str       # "youtube" | "bilibili"
    channel_id: str     # UC... or UID
    channel_name: str   # 显示用
    video_id: str       # YT 11 位 id 或 Bilibili BV 号
    title: str          # 视频标题（原语言）
    url: str            # 落地页 URL
    embed_url: str      # 嵌入 URL
    published: str      # YYYY-MM-DD
    thumbnail: str = "" # 封面图 URL（YT 有，Bili 暂无）
    description: str = ""


# ----- 单频道拉取 ------------------------------------------------------------

def _safe_get(url: str, *, timeout: float = TIMEOUT) -> str:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA})
        if r.status_code >= 400:
            return ""
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        log.debug("fetch %s failed: %s", url, e)
        return ""


def _parse_iso_date(s: str) -> str:
    """Atom 的发布时间是 ISO8601，截取 YYYY-MM-DD 即可。"""
    if not s:
        return ""
    return s[:10]


def fetch_youtube_channel(channel_id: str, channel_name: str, *,
                          limit: int = 8) -> list[VideoItem]:
    """拉一个 YouTube 频道最近 N 条视频。"""
    html = _safe_get(YT_FEED_URL.format(cid=channel_id))
    if not html:
        return []
    try:
        root = ET.fromstring(html)
    except ET.ParseError as e:
        log.warning("YT feed parse failed for %s: %s", channel_id, e)
        return []
    entries = root.findall("atom:entry", NS)
    out: list[VideoItem] = []
    for e in entries[:limit]:
        vid_el = e.find("yt:videoId", NS)
        title_el = e.find("atom:title", NS)
        link_el = e.find("atom:link", NS)
        pub_el = e.find("atom:published", NS)
        media = e.find("media:group", NS)
        if vid_el is None or title_el is None:
            continue
        vid = vid_el.text or ""
        title = (title_el.text or "").strip()
        url = (link_el.get("href") if link_el is not None else "") or f"https://www.youtube.com/watch?v={vid}"
        published = _parse_iso_date(pub_el.text if pub_el is not None else "")
        thumbnail = ""
        desc = ""
        if media is not None:
            th = media.find("media:thumbnail", NS)
            if th is not None:
                thumbnail = th.get("url", "")
            d = media.find("media:description", NS)
            if d is not None:
                desc = (d.text or "").strip()[:600]
        out.append(VideoItem(
            platform="youtube",
            channel_id=channel_id,
            channel_name=channel_name,
            video_id=vid,
            title=title,
            url=url,
            embed_url=f"https://www.youtube.com/embed/{vid}",
            published=published,
            thumbnail=thumbnail,
            description=desc,
        ))
    return out


# ---------- B 站 WBI 签名 -----------------------------------------------------
# 摘要：B 站从 2023 年起所有"敏感"接口都要 WBI 签名。算法：
#   1. 从 /x/web-interface/nav 拿到 img_url / sub_url，提取 img_key / sub_key
#   2. 把 img_key + sub_key 拼接，按官方公布的 64 位索引乱序，前 32 位 = mixin_key
#   3. 请求参数加上 wts (timestamp)，按 key 排序，去掉非法字符后拼成 query 字符串
#   4. w_rid = md5(query + mixin_key)
# 实际跑下来比写出来听上去简单，单文件 60 行就够了。
_WBI_MIXIN_INDEX = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_BILI_KEYS: dict[str, str] = {}      # cached img_key / sub_key
_BILI_SESSION: requests.Session | None = None  # singleton session with buvid3


def _get_bili_session() -> requests.Session:
    """初始化一次 B 站 Session：先访问首页拿 buvid3 cookie，否则 -412。"""
    global _BILI_SESSION
    if _BILI_SESSION is not None:
        return _BILI_SESSION
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA_BROWSER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    # 这两步是关键：必须先有 buvid3 cookie，B 站才不会判定我们是脚本。
    try:
        s.get("https://api.bilibili.com/x/frontend/finger/spi",
              headers={"Referer": "https://www.bilibili.com/"}, timeout=TIMEOUT)
        s.get("https://www.bilibili.com/",
              headers={"Referer": "https://www.bilibili.com/"}, timeout=TIMEOUT)
    except Exception as e:
        log.warning("bilibili session bootstrap failed: %s", e)
    _BILI_SESSION = s
    return s


def _fetch_wbi_keys() -> tuple[str, str]:
    """拉一次 /nav 拿 img_key + sub_key（缓存到 _BILI_KEYS）。失败就抛。"""
    if _BILI_KEYS.get("img") and _BILI_KEYS.get("sub"):
        return _BILI_KEYS["img"], _BILI_KEYS["sub"]
    sess = _get_bili_session()
    r = sess.get(
        BILI_NAV_URL,
        timeout=TIMEOUT,
        headers={"Referer": "https://www.bilibili.com/"},
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    wbi = data.get("wbi_img", {})
    img_url = wbi.get("img_url", "")
    sub_url = wbi.get("sub_url", "")
    img_key = img_url.rsplit("/", 1)[-1].split(".")[0] if img_url else ""
    sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0] if sub_url else ""
    if not img_key or not sub_key:
        raise RuntimeError("could not extract wbi keys from /nav response")
    _BILI_KEYS["img"], _BILI_KEYS["sub"] = img_key, sub_key
    return img_key, sub_key


def _wbi_sign(params: dict) -> dict:
    """给 params 加 wts + w_rid 字段，返回新 dict。"""
    img_key, sub_key = _fetch_wbi_keys()
    raw = img_key + sub_key
    mixin_key = "".join(raw[i] for i in _WBI_MIXIN_INDEX if i < len(raw))[:32]
    params = dict(params)
    params["wts"] = int(time.time())
    # 排序 + 去 !'()* 等非法字符
    sorted_pairs = sorted(params.items())
    cleaned = [(k, re.sub(r"[!'()*]", "", str(v))) for k, v in sorted_pairs]
    query = urllib.parse.urlencode(cleaned)
    w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
    params["w_rid"] = w_rid
    return params


def fetch_bilibili_channel(uid: str, channel_name: str, *,
                           limit: int = 8) -> list[VideoItem]:
    """拉一个 B 站 UP 主最近 N 条视频（直接走官方 API + WBI 签名）。"""
    try:
        params = _wbi_sign({"mid": uid, "pn": 1, "ps": limit, "order": "pubdate"})
        sess = _get_bili_session()
        r = sess.get(
            BILI_SPACE_URL,
            params=params,
            timeout=TIMEOUT,
            headers={
                "Referer": f"https://space.bilibili.com/{uid}",
                "Accept": "application/json",
            },
        )
        data = r.json()
    except Exception as e:
        log.warning("bilibili wbi call failed for uid=%s: %s", uid, e)
        return []
    if data.get("code") != 0:
        log.warning("bilibili api error uid=%s: code=%s msg=%s",
                    uid, data.get("code"), data.get("message"))
        return []
    vlist = (data.get("data") or {}).get("list", {}).get("vlist") or []
    out: list[VideoItem] = []
    for x in vlist[:limit]:
        bv = x.get("bvid") or ""
        title = (x.get("title") or "").strip()
        created = x.get("created") or 0
        published = (
            dt.datetime.fromtimestamp(int(created)).strftime("%Y-%m-%d")
            if created else ""
        )
        if not bv:
            continue
        out.append(VideoItem(
            platform="bilibili",
            channel_id=uid,
            channel_name=channel_name,
            video_id=bv,
            title=title,
            url=f"https://www.bilibili.com/video/{bv}",
            embed_url=f"https://player.bilibili.com/player.html?bvid={bv}&high_quality=1&autoplay=0",
            published=published,
            thumbnail=x.get("pic") or "",
            description=(x.get("description") or "")[:600],
        ))
    return out


# ----- VideoItem → Paper 卡 ----------------------------------------------------

def video_to_paper(v: VideoItem) -> Paper:
    """把视频包装成 Paper 卡片对象。"""
    # 复合 id：vid-<youtube|bili>-<video_id>，保证全局唯一且稳定
    pid_key = f"{v.platform}-{v.video_id}"
    pid = f"vid-{pid_key}"
    # 没有 abs_url 就用 url 顶；没有 abstract 就用 description
    p = Paper(
        id=pid,
        title=v.title,
        title_zh=v.title,            # 标题已是源语言；翻译留给主 pipeline
        abstract=v.description or v.title,
        abstract_zh="",
        tldr_zh="",
        cover_zh="",
        published=v.published or dt.date.today().isoformat(),
        primary_category="video",
        categories=[],
        authors=[],
        source=f"video_{v.platform}",
        abs_url=v.url,
        pdf_url="",
        channels=[],                  # 等会儿在 build pipeline 里按关键词归类
    )
    # 视频本身就是 demo
    p.demo_videos = [{
        "kind": v.platform,           # "youtube" | "bilibili"
        "url": v.url,
        "embed_url": v.embed_url,
        "title": v.title,
        "source": "channel",
    }]
    # 视频频道作为 institution 标签直接显示
    p.institutions = [v.channel_name]
    return p


# ----- 主接口 ------------------------------------------------------------------

# 内置频道清单。用户可在 config/sources.yaml 里加 `video_channels.extra` 追加。
# 注意：这里的 ID 已通过 youtube.com/channel/UCxxx 与 B 站 api 验证过。
BUILT_IN_CHANNELS: list[dict] = [
    # ----- YouTube：海外人形机器人厂商 -----
    {"platform": "youtube", "id": "UC7vVhkEfw4nOGp8TyDk7RcQ", "name": "Boston Dynamics"},
    {"platform": "youtube", "id": "UCsMbp4V8oxzHCMdOUP-3oWw", "name": "Unitree Robotics"},
    {"platform": "youtube", "id": "UCYlq-KmwPjc1DtsGmthFqSQ", "name": "Figure"},
    {"platform": "youtube", "id": "UCSCB0UN0Xh4UD9YxtzcjLvg", "name": "1X Tech"},
    # ----- Bilibili：国内具身机器人圈大号 -----
    {"platform": "bilibili", "id": "521974986", "name": "宇树科技"},
    {"platform": "bilibili", "id": "3494380742642452", "name": "智元AGIBOT"},
    {"platform": "bilibili", "id": "673779175", "name": "量子位"},
]


def fetch_all_video_channels(*, limit_per_channel: int = 8,
                              channels: list[dict] | None = None,
                              max_age_days: int = 60) -> list[Paper]:
    """拉所有视频频道，返回 Paper 卡列表。

    Args:
        limit_per_channel: 每个频道拿最新 N 条。
        channels: 频道清单；不传则用 BUILT_IN_CHANNELS。
        max_age_days: 过老的视频丢掉（默认 60 天）。

    Pipeline 集成方式：在 build.run_pipeline 里调一次，把结果合进 `fresh`。
    """
    use = channels if channels is not None else BUILT_IN_CHANNELS
    cutoff = dt.date.today() - dt.timedelta(days=max_age_days) if max_age_days else None
    out: list[Paper] = []
    for ch in use:
        try:
            if ch["platform"] == "youtube":
                items = fetch_youtube_channel(ch["id"], ch["name"], limit=limit_per_channel)
            elif ch["platform"] == "bilibili":
                items = fetch_bilibili_channel(ch["id"], ch["name"], limit=limit_per_channel)
            else:
                continue
        except Exception as e:
            log.warning("video channel %s/%s fetch failed: %s", ch["platform"], ch["name"], e)
            continue
        log.info("video %s: %s → %d items", ch["platform"], ch["name"], len(items))
        for v in items:
            if cutoff and v.published:
                try:
                    d = dt.date.fromisoformat(v.published)
                except ValueError:
                    d = None
                if d and d < cutoff:
                    continue
            out.append(video_to_paper(v))
        # 防止把 rsshub 打爆 / YT 限速
        time.sleep(0.3)
    log.info("video channels: %d total Paper cards", len(out))
    return out


__all__ = [
    "VideoItem",
    "fetch_youtube_channel",
    "fetch_bilibili_channel",
    "fetch_all_video_channels",
    "video_to_paper",
    "BUILT_IN_CHANNELS",
]
