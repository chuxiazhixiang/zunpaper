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
    aff_hay = "\n".join(a.affiliation for a in paper.authors if a.affiliation)
    aff_hay += "\n" + (paper.abstract or "")
    for lab_rule in labs_mod._LAB_RULES or []:
        for pat in lab_rule["patterns"]:
            if _substr_or_regex(aff_hay, pat):
                return {"points": rule["points"], "hint": f'命中 “{lab_rule["label"]}” 关键词'}
    return 0


def _check_key_author(paper: Paper, rule: dict) -> dict | int:
    labs_mod._ensure_loaded()
    author_hay = "\n".join(a.name for a in paper.authors if a.name)
    for au_rule in labs_mod._AUTHOR_RULES or []:
        for pat in au_rule["patterns"]:
            if _substr_or_regex(author_hay, pat):
                return {"points": rule["points"], "hint": f'作者列表里有 {au_rule["label"]}'}
    return 0


def _check_hf_trending(paper: Paper, rule: dict) -> dict | int:
    if (paper.source or "").lower() == "hf_daily":
        return rule["points"]
    if "hf_daily" in (paper.source_tags or []):
        return rule["points"]
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
    if n >= 8:
        return {"points": rule["points"], "hint": f"PDF {n} 页"}
    return 0


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
    "keyword_density": _check_keyword_density,
    "freshness": _check_freshness,
    "github_repo": _check_github_repo,
    "longer_paper": _check_longer_paper,
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
