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
    # Normalize entries: each is { label: str, patterns: [str, ...] }
    def _norm(items):
        out = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            label = (entry.get("label") or "").strip()
            patterns = entry.get("patterns") or []
            if not label or not patterns:
                continue
            out.append({"label": label, "patterns": [str(p) for p in patterns]})
        return out
    return _norm(labs), _norm(authors)


# Cache the parsed config so we don't hit disk per-paper.
_LAB_RULES: list[dict] | None = None
_AUTHOR_RULES: list[dict] | None = None


def _ensure_loaded() -> None:
    global _LAB_RULES, _AUTHOR_RULES
    if _LAB_RULES is None or _AUTHOR_RULES is None:
        _LAB_RULES, _AUTHOR_RULES = _load_rules()


def detect_labs(paper: Paper) -> list[str]:
    """Return a deduped list of badge labels matched on this paper.

    Combines both "famous lab" (affiliation/abstract scan) and
    "key author" (author-name scan) hits.
    """
    _ensure_loaded()
    hay_parts: list[str] = []
    hay_parts.extend(a.affiliation for a in paper.authors if a.affiliation)
    hay_parts.append(paper.abstract or "")
    aff_hay = "\n".join(hay_parts)
    author_hay = "\n".join(a.name for a in paper.authors if a.name)

    out: list[str] = []
    for rule in _LAB_RULES or []:
        if any(_match(aff_hay, p) for p in rule["patterns"]) and rule["label"] not in out:
            out.append(rule["label"])
    for rule in _AUTHOR_RULES or []:
        if any(_match(author_hay, p) for p in rule["patterns"]) and rule["label"] not in out:
            out.append(rule["label"])
    return out


def lab_badges(labs: Iterable[str]) -> list[dict[str, str]]:
    return [{"kind": "lab", "label": f"⭐ {l}"} for l in labs]
