"""Detect "famous lab" + "key author" badges on a Paper.

The rule set lives in `config/famous_labs.yaml` so it can be tuned by the
site owner without touching code. Two sections:

  labs:    matched against affiliation + abstract
  authors: matched against author names

Match is case-insensitive substring (or regex, see `_match`). False
negatives are fine — the badge is a bonus, not a filter.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

import yaml

from .models import Paper

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "famous_labs.yaml"


def _match(text: str, pattern: str) -> bool:
    """Return True if `pattern` matches inside `text`.

    Plain strings are matched as case-insensitive substrings. If the pattern
    is surrounded by `/.../` it's treated as a (case-insensitive) regex.
    """
    if not text or not pattern:
        return False
    if pattern.startswith("/") and pattern.endswith("/") and len(pattern) > 2:
        try:
            return re.search(pattern[1:-1], text, re.IGNORECASE) is not None
        except re.error:
            return False
    return pattern.lower() in text.lower()


def _load_rules() -> tuple[list[dict], list[dict]]:
    if not _CONFIG_PATH.exists():
        log.warning("famous_labs.yaml not found at %s; no lab badges will fire", _CONFIG_PATH)
        return [], []
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as e:
        log.warning("Failed to parse famous_labs.yaml: %s", e)
        return [], []
    labs = data.get("labs") or []
    authors = data.get("authors") or []
    # Normalize entries: each is
    #   { label: str, patterns: [str, ...], require_affiliation?: [str, ...] }
    def _norm(items):
        out = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            label = (entry.get("label") or "").strip()
            patterns = entry.get("patterns") or []
            if not label or not patterns:
                continue
            norm = {"label": label, "patterns": [str(p) for p in patterns]}
            # 可选机构守卫：只用于作者规则，给重名作者消歧（见 author_rule_matches）。
            req = entry.get("require_affiliation")
            if req:
                norm["require_affiliation"] = [str(p) for p in req]
            out.append(norm)
        return out
    return _norm(labs), _norm(authors)


# Cache the parsed config so we don't hit disk per-paper.
_LAB_RULES: list[dict] | None = None
_AUTHOR_RULES: list[dict] | None = None


def _ensure_loaded() -> None:
    global _LAB_RULES, _AUTHOR_RULES
    if _LAB_RULES is None or _AUTHOR_RULES is None:
        _LAB_RULES, _AUTHOR_RULES = _load_rules()


def affiliation_haystack(paper: Paper) -> str:
    """All text we trust to carry institution signals: per-author affiliation
    (often empty for arXiv), the LLM-extracted `institutions`, and the abstract.

    历史坑：以前只看 author.affiliation + abstract，而 arXiv 抓下来的论文
    per-author affiliation 基本是空的，真正的机构信息在 enrich.py 抽出来的
    `paper.institutions` 里。不把 institutions 算进来，机构守卫（重名消歧）
    和 ZJU/USC 这类高校 lab 徽章就几乎永远命中不了。"""
    parts: list[str] = []
    parts.extend(a.affiliation for a in paper.authors if a.affiliation)
    parts.extend(paper.institutions or [])
    parts.append(paper.abstract or "")
    return "\n".join(parts)


def author_haystack(paper: Paper) -> str:
    return "\n".join(a.name for a in paper.authors if a.name)


def author_rule_matches(rule: dict, author_hay: str, aff_hay: str) -> bool:
    """An author rule fires when a name pattern hits AND (if the rule declares
    `require_affiliation`) at least one affiliation guard also hits.

    机构守卫专治常见重名：例如「Yue Wang」在 USC（机器人 AP）和浙大
    （ywang-zju，人形/具身）各有一位，只按名字子串匹配会互相误标。给两条
    规则分别声明 require_affiliation，必须在机构/摘要里看到对应学校才打徽章；
    都看不到就宁可不打（漏标 < 错标）。"""
    if not any(_match(author_hay, p) for p in rule["patterns"]):
        return False
    guards = rule.get("require_affiliation") or []
    if guards and not any(_match(aff_hay, g) for g in guards):
        return False
    return True


def detect_labs(paper: Paper) -> list[str]:
    """Return a deduped list of badge labels matched on this paper.

    Combines both "famous lab" (affiliation/abstract scan) and
    "key author" (author-name scan, with optional affiliation guard) hits.
    """
    _ensure_loaded()
    aff_hay = affiliation_haystack(paper)
    author_hay = author_haystack(paper)

    out: list[str] = []
    for rule in _LAB_RULES or []:
        if any(_match(aff_hay, p) for p in rule["patterns"]) and rule["label"] not in out:
            out.append(rule["label"])
    for rule in _AUTHOR_RULES or []:
        if rule["label"] in out:
            continue
        if author_rule_matches(rule, author_hay, aff_hay):
            out.append(rule["label"])
    return out


def lab_badges(labs: Iterable[str]) -> list[dict[str, str]]:
    return [{"kind": "lab", "label": f"⭐ {l}"} for l in labs]
