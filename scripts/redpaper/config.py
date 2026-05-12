"""Load YAML configs from the config/ directory."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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


@dataclass
class SiteConfig:
    title: str = "redpaper"
    subtitle: str = ""
    author: str = ""
    primary_color: str = "#FF2442"
    accent_color: str = "#FF6B8A"
    feed_page_size: int = 60
    default_channel: str = ""
    translation_backend_env: str = "REDPAPER_LLM_BACKEND"
    translation_default_backend: str = "dryrun"
    translation_cache_dir: str = "site/data/papers"


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_channels() -> list[Channel]:
    raw = _load_yaml(CONFIG_DIR / "channels.yaml")
    return [Channel(**c) for c in raw.get("channels", [])]


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
        translation_backend_env=tr.get("backend_env", "REDPAPER_LLM_BACKEND"),
        translation_default_backend=tr.get("default_backend", "dryrun"),
        translation_cache_dir=tr.get("cache_dir", "site/data/papers"),
    )


def ensure_dirs() -> None:
    for d in (DATA_DIR, PAPERS_DIR, DAILY_DIR, COVER_DIR):
        d.mkdir(parents=True, exist_ok=True)
