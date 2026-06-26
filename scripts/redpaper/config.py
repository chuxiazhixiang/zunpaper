"""Load YAML configs from the config/ directory."""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
SITE_DIR = REPO_ROOT / "site"
DATA_DIR = SITE_DIR / "data"
PAPERS_DIR = DATA_DIR / "papers"
DAILY_DIR = DATA_DIR / "daily"
ASSETS_DIR = SITE_DIR / "assets"
COVER_DIR = ASSETS_DIR / "img" / "covers"


@dataclass
class Channel:
    id: str
    name: str
    emoji: str = ""
    arxiv_categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    max_per_day: int = 30
    # ---- 自定义分类（config/channels.d/*.yaml 拖入）专用字段 -------------
    # desc / judge_prompt 同时也用于 judge：有 judge_prompt 的频道走「B 方案」——
    # 用它自己这段 prompt 独立判定（只判命中本频道关键词的论文），不受核心 6 类
    # 那段 SYSTEM_PROMPT 的黑名单（如「不要医疗」）影响。详见 judge.judge_paper_for_channel。
    desc: str = ""                                   # 一句话方向定义（人 / LLM 都看）
    judge_prompt: str = ""                            # 站长自己写的「什么算 / 不算这个方向」
    venues: list[str] = field(default_factory=list)   # 关注的会议 / 期刊 → 评分加分
    examples: list[dict] = field(default_factory=list)  # [{title, url|id}] 示例高质量论文 → pin + few-shot
    backfill_days: int = 0                            # 首次激活时回填最近 N 天 arxiv（0=不额外回填）
    custom: bool = False                              # 是否来自 channels.d 的自定义分类

    @property
    def is_custom(self) -> bool:
        """自定义分类 = 显式标记 custom 或带了独立 judge_prompt。
        这类频道走独立 prompt 判定，核心 6 类不受影响。"""
        return bool(self.custom or self.judge_prompt)


@dataclass
class SourcesConfig:
    arxiv_enabled: bool = True
    arxiv_lookback_days: int = 2
    arxiv_per_channel_limit: int = 60
    # "Evergreen 回补"：当今日窗口拉出来的论文数 < min_papers 时，
    # 自动把 lookback 放大到 fallback_days 再补抓一次。
    arxiv_evergreen_min_papers: int = 0
    arxiv_evergreen_fallback_days: int = 30
    hf_daily_enabled: bool = False
    semantic_scholar_enabled: bool = False
    alphaxiv_enabled: bool = False
    qbitai_enabled: bool = False
    qbitai_lookback_days: int = 30
    jiqizhixin_enabled: bool = False
    embodied_techdaily_enabled: bool = False
    embodied_techdaily_lookback_days: int = 60
    shenlan_embodied_enabled: bool = False
    shenlan_embodied_lookback_days: int = 60
    manual_xhs_enabled: bool = False
    manual_arxiv_enabled: bool = False
    # P5: 视频频道源
    video_channels_enabled: bool = True
    video_per_channel: int = 6
    video_lookback_days: int = 30
    # P7: LLM 联网发现（discover.py）—— Gemini grounded search 主动找新论文
    discover_enabled: bool = True
    discover_lookback_days: int = 14
    discover_per_channel: int = 5
    # GitHub 开源项目栏目（github_repos.py）—— 高 star + AI 判定的具身/人形算法仓
    github_enabled: bool = True
    github_min_stars: int = 300
    github_max_repos: int = 120
    github_refresh_days: int = 7
    # 会议官网源（OpenReview：CoRL/ICLR/NeurIPS 等接收论文）
    openreview_enabled: bool = True
    openreview_venue_ids: list[str] = field(default_factory=list)
    openreview_max_per_venue: int = 80
    openreview_refresh_days: int = 14
    # 会议/期刊源（Semantic Scholar 按 venue 检索：CVPR/ICCV/ECCV/RA-L/ICML 等）
    conf_papers_enabled: bool = True
    conf_papers_venues: list[dict] = field(default_factory=list)
    conf_papers_max_per_venue: int = 40
    conf_papers_refresh_days: int = 14


@dataclass
class SiteConfig:
    title: str = "redpaper"
    subtitle: str = ""
    author: str = ""
    primary_color: str = "#FF2442"
    accent_color: str = "#FF6B8A"
    feed_page_size: int = 60
    default_channel: str = ""
    goatcounter: str = ""        # GoatCounter code（子域名前缀）；空=不统计访问
    translation_backend_env: str = "REDPAPER_LLM_BACKEND"
    translation_default_backend: str = "dryrun"
    translation_cache_dir: str = "site/data/papers"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _channel_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(Channel)}


def _channel_from_dict(d: dict[str, Any], *, custom: bool = False) -> Channel:
    """从 dict 安全构造 Channel：只取 dataclass 已知字段，忽略多余 key
    （自定义分类文件里可能带注释性的额外字段），避免 `Channel(**d)` 因未知
    键直接抛错。"""
    known = _channel_field_names()
    kw = {k: v for k, v in d.items() if k in known}
    if custom:
        kw["custom"] = True
    return Channel(**kw)


def load_channels() -> list[Channel]:
    """核心 6 类来自 config/channels.yaml；再合并 config/channels.d/*.yaml 里
    每个「自定义分类」文件（一个文件 = 一个频道，零格式风险，小白拖进去即可）。

    自定义文件格式（任选其一）：
      - 顶层就是频道字段：{id, name, emoji, keywords, desc, judge_prompt, ...}
      - 或 {channels: [ {...}, ... ]}（与 channels.yaml 同构）
    id 与已有频道冲突的会被跳过（核心配置优先）。
    """
    raw = _load_yaml(CONFIG_DIR / "channels.yaml")
    chans = [_channel_from_dict(c) for c in raw.get("channels", [])]
    seen = {c.id for c in chans}

    extra_dir = CONFIG_DIR / "channels.d"
    if extra_dir.is_dir():
        for fp in sorted(extra_dir.glob("*.yaml")) + sorted(extra_dir.glob("*.yml")):
            try:
                data = _load_yaml(fp)
            except Exception as e:
                log.warning("channels.d: skip %s (parse failed: %s)", fp.name, e)
                continue
            if not isinstance(data, dict):
                continue
            items = data.get("channels") if "channels" in data else [data]
            for item in items or []:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    log.warning("channels.d: %s 缺少 id/name，跳过", fp.name)
                    continue
                if item["id"] in seen:
                    log.warning("channels.d: id '%s' 与已有频道冲突，跳过 %s", item["id"], fp.name)
                    continue
                try:
                    chans.append(_channel_from_dict(item, custom=True))
                    seen.add(item["id"])
                    log.info("channels.d: loaded custom channel '%s' from %s", item["id"], fp.name)
                except Exception as e:
                    log.warning("channels.d: build channel failed for %s: %s", fp.name, e)

    return chans


_ARXIV_NUM_RE = __import__("re").compile(r"^(\d{4})\.(\d{4,6})$")


def _normalize_paper_id(raw: str) -> str:
    """把 '2606.12366' 这种 arxiv 号归一成 slug 'arxiv-2606-12366'；其它原样返回。"""
    s = str(raw or "").strip()
    m = _ARXIV_NUM_RE.match(s)
    if m:
        return f"arxiv-{m.group(1)}-{m.group(2)}"
    return s


def load_curated_ids() -> set[str]:
    """站长甄选高质量清单（config/curated.yaml）。返回归一化后的 paper id 集合。
    条目可写 `- id: arxiv-...` / `- id: 2606.12366` / 直接 `- arxiv-...`。"""
    path = CONFIG_DIR / "curated.yaml"
    if not path.exists():
        return set()
    raw = _load_yaml(path).get("curated") or []
    out: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            pid = item.get("id")
        else:
            pid = item
        if pid:
            out.add(_normalize_paper_id(pid))
    return out


def load_conferences() -> list[dict]:
    """读 config/conferences.yaml → 会议倒计时数据（原样透传给前端）。"""
    path = CONFIG_DIR / "conferences.yaml"
    if not path.exists():
        return []
    try:
        raw = _load_yaml(path)
    except Exception as e:
        log.warning("conferences.yaml parse failed: %s", e)
        return []
    out = []
    for c in raw.get("conferences", []) or []:
        if isinstance(c, dict) and c.get("name"):
            out.append(c)
    return out


def load_sources() -> SourcesConfig:
    import os

    raw = _load_yaml(CONFIG_DIR / "sources.yaml").get("sources", {})

    def get(name: str, key: str, default):
        return raw.get(name, {}).get(key, default)

    lookback = get("arxiv", "lookback_days", 7)
    # Allow env override for one-off backfills (workflow_dispatch input maps
    # to REDPAPER_ARXIV_LOOKBACK_DAYS).
    env_override = (os.environ.get("REDPAPER_ARXIV_LOOKBACK_DAYS") or "").strip()
    if env_override:
        try:
            lookback = int(env_override)
        except ValueError:
            pass

    return SourcesConfig(
        arxiv_enabled=get("arxiv", "enabled", True),
        arxiv_lookback_days=lookback,
        arxiv_per_channel_limit=get("arxiv", "per_channel_limit", 60),
        arxiv_evergreen_min_papers=get("arxiv", "evergreen_min_papers", 0),
        arxiv_evergreen_fallback_days=get("arxiv", "evergreen_fallback_days", 30),
        hf_daily_enabled=get("hf_daily", "enabled", False),
        semantic_scholar_enabled=get("semantic_scholar", "enabled", False),
        alphaxiv_enabled=get("alphaxiv", "enabled", False),
        qbitai_enabled=get("qbitai", "enabled", False),
        qbitai_lookback_days=get("qbitai", "lookback_days", 30),
        jiqizhixin_enabled=get("jiqizhixin", "enabled", False),
        embodied_techdaily_enabled=get("embodied_techdaily", "enabled", False),
        embodied_techdaily_lookback_days=get("embodied_techdaily", "lookback_days", 60),
        shenlan_embodied_enabled=get("shenlan_embodied", "enabled", False),
        shenlan_embodied_lookback_days=get("shenlan_embodied", "lookback_days", 60),
        manual_xhs_enabled=get("manual_xhs", "enabled", False),
        manual_arxiv_enabled=get("manual_arxiv", "enabled", False),
        video_channels_enabled=get("video_channels", "enabled", True),
        video_per_channel=get("video_channels", "per_channel", 6),
        video_lookback_days=get("video_channels", "lookback_days", 30),
        discover_enabled=get("discover", "enabled", True),
        discover_lookback_days=get("discover", "lookback_days", 14),
        discover_per_channel=get("discover", "per_channel", 5),
        github_enabled=get("github", "enabled", True),
        github_min_stars=get("github", "min_stars", 300),
        github_max_repos=get("github", "max_repos", 120),
        github_refresh_days=get("github", "refresh_days", 7),
        openreview_enabled=get("openreview", "enabled", True),
        openreview_venue_ids=get("openreview", "venue_ids", []) or [],
        openreview_max_per_venue=get("openreview", "max_per_venue", 80),
        openreview_refresh_days=get("openreview", "refresh_days", 14),
        conf_papers_enabled=get("conf_papers", "enabled", True),
        conf_papers_venues=get("conf_papers", "venues", []) or [],
        conf_papers_max_per_venue=get("conf_papers", "max_per_venue", 40),
        conf_papers_refresh_days=get("conf_papers", "refresh_days", 14),
    )


def load_site() -> SiteConfig:
    raw = _load_yaml(CONFIG_DIR / "site.yaml")
    site = raw.get("site", {})
    tr = raw.get("translation", {})
    return SiteConfig(
        title=site.get("title", "redpaper"),
        subtitle=site.get("subtitle", ""),
        author=site.get("author", ""),
        primary_color=site.get("primary_color", "#FF2442"),
        accent_color=site.get("accent_color", "#FF6B8A"),
        feed_page_size=site.get("feed_page_size", 60),
        default_channel=site.get("default_channel", ""),
        goatcounter=site.get("goatcounter", "") or "",
        translation_backend_env=tr.get("backend_env", "REDPAPER_LLM_BACKEND"),
        translation_default_backend=tr.get("default_backend", "dryrun"),
        translation_cache_dir=tr.get("cache_dir", "site/data/papers"),
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, PAPERS_DIR, DAILY_DIR, COVER_DIR):
        d.mkdir(parents=True, exist_ok=True)
