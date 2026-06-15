"""Per-paper "why we picked you" scoring.

Reads `config/scoring.yaml` for the rule weights, then for each paper
evaluates each rule (`check_<rule_id>` below) and returns:

  score (int)         total summed points
  breakdown (list)    [{ "id", "label", "points", "hint" }, ...]
                      only the rules that fired show up in the breakdown.

The output is attached to each paper so the frontend can render a
"为啥今天选了它" section transparently.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

import yaml

from . import labs as labs_mod
from .models import Paper

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "scoring.yaml"

_GITHUB_RE = re.compile(r"github\.com|huggingface\.co", re.IGNORECASE)


def _load_rules() -> list[dict]:
    if not _CONFIG_PATH.exists():
        log.warning("scoring.yaml not found at %s; everyone scores 0", _CONFIG_PATH)
        return []
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as e:
        log.warning("Failed to parse scoring.yaml: %s", e)
        return []
    rules = data.get("rules") or []
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if not rid:
            continue
        out.append({
            "id": rid,
            "label": r.get("label", rid),
            "points": int(r.get("points", 0)),
            "hint": r.get("hint", ""),
        })
    return out


_RULES_CACHE: list[dict] | None = None


def _rules() -> list[dict]:
    global _RULES_CACHE
    if _RULES_CACHE is None:
        _RULES_CACHE = _load_rules()
    return _RULES_CACHE


# ---- Per-rule evaluators ---------------------------------------------------
# Each returns either:
#   - 0  not a hit
#   - int (positive)  hit, and this is the actual points to award (lets the
#                     rule scale, e.g. keyword_density × count)
#   - dict { "points": int, "hint": str }   to override the hint with paper-
#                     specific info (e.g. which lab matched)

def _check_manual_pin(paper: Paper, rule: dict) -> dict | int:
    """User manually pinned this paper via config/manual_arxiv.yaml."""
    if (paper.source or "").lower() == "manual_arxiv":
        return rule["points"]
    if "manual_pin" in (paper.source_tags or []):
        return rule["points"]
    return 0


def _check_famous_lab(paper: Paper, rule: dict) -> dict | int:
    labs_mod._ensure_loaded()
    # 复用 labs 的机构 haystack（含 enrich 抽的 institutions），避免逻辑漂移。
    aff_hay = labs_mod.affiliation_haystack(paper)
    for lab_rule in labs_mod._LAB_RULES or []:
        for pat in lab_rule["patterns"]:
            if _substr_or_regex(aff_hay, pat):
                return {"points": rule["points"], "hint": f'命中 “{lab_rule["label"]}” 关键词'}
    return 0


def _check_key_author(paper: Paper, rule: dict) -> dict | int:
    labs_mod._ensure_loaded()
    # 走 labs.author_rule_matches，让机构守卫（重名消歧）在打分侧也生效，
    # 否则会出现「徽章没打、分数却加了」的不一致。
    author_hay = labs_mod.author_haystack(paper)
    aff_hay = labs_mod.affiliation_haystack(paper)
    for au_rule in labs_mod._AUTHOR_RULES or []:
        if labs_mod.author_rule_matches(au_rule, author_hay, aff_hay):
            return {"points": rule["points"], "hint": f'作者列表里有 {au_rule["label"]}'}
    return 0


def _check_hf_trending(paper: Paper, rule: dict) -> dict | int:
    if (paper.source or "").lower() == "hf_daily":
        return rule["points"]
    if "hf_daily" in (paper.source_tags or []):
        return rule["points"]
    return 0


_MEDIA_SOURCES = {
    "qbitai", "jiqizhixin",
    "embodied_techdaily", "shenlan_embodied",
    # P5: video channel sources also count as "high-signal curated" media —
    # 这些都是厂商官方发布的 demo，跟公众号一样属于"圈内人筛过的"。
    "video_youtube", "video_bilibili",
}

_SOURCE_LABEL = {
    "qbitai": "量子位",
    "jiqizhixin": "机器之心",
    "embodied_techdaily": "具身智能之心",
    "shenlan_embodied": "深蓝具身智能",
    "video_youtube": "YouTube 视频",
    "video_bilibili": "B 站视频",
}


def _check_from_media(paper: Paper, rule: dict) -> dict | int:
    src = (paper.source or "").lower()
    if src in _MEDIA_SOURCES:
        return {"points": rule["points"],
                "hint": f"来自 {_SOURCE_LABEL.get(src, src)}"}
    return 0


def _check_keyword_density(paper: Paper, rule: dict) -> dict | int:
    text = ((paper.title or "") + " " + (paper.abstract or "")).lower()
    # Pull the actual channel keyword lists from channels.yaml and count
    # how many distinct keywords from this paper's channels appear in
    # title+abstract. More hits = stronger topical match.
    from .config import load_channels
    try:
        channels = {c.id: c for c in load_channels()}
    except Exception:
        channels = {}
    hits = 0
    matched_words = []
    for ch_id in (paper.channels or []):
        ch = channels.get(ch_id)
        if not ch:
            continue
        for kw in (ch.keywords or []):
            if kw.lower() in text and kw.lower() not in matched_words:
                hits += 1
                matched_words.append(kw.lower())
    pts = min(rule["points"], hits * 3)
    if pts <= 0:
        return 0
    sample = "、".join(matched_words[:4])
    return {"points": pts, "hint": f"频道关键词命中 {hits} 个（{sample}）"}


def _check_freshness(paper: Paper, rule: dict) -> dict | int:
    from datetime import datetime, timezone
    if not paper.published:
        return 0
    try:
        # paper.published may be ISO date or full timestamp.
        pub = datetime.fromisoformat(paper.published.replace("Z", "+00:00"))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    age_days = (datetime.now(timezone.utc) - pub).days
    if age_days <= 2:
        return {"points": rule["points"], "hint": f"投递于 {age_days} 天前"}
    return 0


def _check_github_repo(paper: Paper, rule: dict) -> dict | int:
    if _GITHUB_RE.search(paper.abstract or ""):
        return rule["points"]
    return 0


def _check_longer_paper(paper: Paper, rule: dict) -> dict | int:
    n = getattr(paper, "page_count", 0) or 0
    if n >= 4:
        return {"points": rule["points"], "hint": f"PDF {n} 页"}
    return 0


_CONF_VENUE_RE = re.compile(
    r"""\b(
        ICRA |
        IROS |
        RSS |
        CoRL |
        RA-?L |          # RAL / RA-L
        T-?RO |          # T-RO
        IJRR |
        ICLR |
        NeurIPS |
        ICML
    )\b""",
    # 移除了 Humanoids?：它会把海量普通 "Humanoid robot" 标题误判成「顶会出品」。
    # IEEE-RAS Humanoids 会议太小众，为它放进来得不偿失。
    re.IGNORECASE | re.VERBOSE,
)


def _check_conf_venue(paper: Paper, rule: dict) -> dict | int:
    hay = f"{paper.title or ''}\n{paper.abstract or ''}"
    m = _CONF_VENUE_RE.search(hay)
    if not m:
        return 0
    return {"points": rule["points"], "hint": f'提到了「{m.group(0)}」'}


def _check_cross_channel(paper: Paper, rule: dict) -> dict | int:
    n = len(paper.channels or [])
    if n >= 2:
        return {"points": rule["points"], "hint": f"同时命中 {n} 个频道"}
    return 0


_EVALUATORS = {
    "manual_pin": _check_manual_pin,
    "famous_lab": _check_famous_lab,
    "key_author": _check_key_author,
    "hf_trending": _check_hf_trending,
    "from_media": _check_from_media,
    "keyword_density": _check_keyword_density,
    "freshness": _check_freshness,
    "github_repo": _check_github_repo,
    "longer_paper": _check_longer_paper,
    "conf_venue": _check_conf_venue,
    "cross_channel": _check_cross_channel,
}


def _substr_or_regex(text: str, pattern: str) -> bool:
    if not text or not pattern:
        return False
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
        try:
            return re.search(pattern[1:-1], text, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def score_paper(paper: Paper) -> tuple[int, list[dict]]:
    """Return (total_score, breakdown_list)."""
    # GitHub 开源仓单独按 star 打分（论文那套规则不适用）。前端「开源项目」tab
    # 实际用真实 star 排序，这里给个 log 归一化分数仅用于展示 / 兜底排序。
    if (paper.source or "") == "github":
        import math
        stars = int((paper.github or {}).get("stars", 0))
        pts = min(100, int(round(20 * math.log10(max(stars, 1)))))  # 1k≈60, 1万≈80, 10万≈100
        return pts, [{
            "id": "github_stars",
            "label": "GitHub 热度",
            "points": pts,
            "hint": f"⭐ {stars}",
        }]
    breakdown: list[dict] = []
    total = 0
    for rule in _rules():
        fn = _EVALUATORS.get(rule["id"])
        if fn is None:
            continue
        try:
            res = fn(paper, rule)
        except Exception as e:
            log.debug("rule %s errored on %s: %s", rule["id"], paper.id, e)
            continue
        if not res:
            continue
        if isinstance(res, dict):
            pts = int(res.get("points", 0))
            hint = res.get("hint", rule.get("hint", ""))
        else:
            pts = int(res)
            hint = rule.get("hint", "")
        if pts == 0:
            continue
        total += pts
        breakdown.append({
            "id": rule["id"],
            "label": rule["label"],
            "points": pts,
            "hint": hint,
        })
    return total, breakdown


def annotate_all(papers: Iterable[Paper]) -> None:
    """Mutate each paper in place: set `.score` and `.score_breakdown`."""
    for p in papers:
        score, breakdown = score_paper(p)
        p.score = score
        p.score_breakdown = breakdown
