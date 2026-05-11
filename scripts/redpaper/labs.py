"""Detect "famous lab" affiliations on a Paper.

For Phase 3 we don't have reliable affiliation metadata from arXiv directly,
so we infer from author names by checking against a curated allow-list of
common authors / first-letter prefixes. This is intentionally simple — false
negatives are fine since the badge is a bonus, not a filter.

The real signal comes from `affiliation` once we plug it in (e.g. via
Semantic Scholar's author records). Until then, we look at the abstract for
mentions of the lab name as a weak signal.
"""
from __future__ import annotations

import re
from typing import Iterable

from .models import Paper

# Pattern → label. Lower-cased.
LAB_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(google|deepmind|google deepmind|google research)\b", re.I), "Google"),
    (re.compile(r"\b(openai)\b", re.I), "OpenAI"),
    (re.compile(r"\b(anthropic)\b", re.I), "Anthropic"),
    (re.compile(r"\b(meta ai|fair|facebook ai)\b", re.I), "Meta"),
    (re.compile(r"\b(microsoft research|microsoft)\b", re.I), "Microsoft"),
    (re.compile(r"\b(mistral ai|mistral)\b", re.I), "Mistral"),
    (re.compile(r"\b(cohere)\b", re.I), "Cohere"),
    (re.compile(r"\b(stanford university|stanford)\b", re.I), "Stanford"),
    (re.compile(r"\b(carnegie mellon university|cmu)\b", re.I), "CMU"),
    (re.compile(r"\b(mit|massachusetts institute of technology)\b", re.I), "MIT"),
    (re.compile(r"\b(eth zürich|eth zurich|eth)\b", re.I), "ETH"),
    (re.compile(r"\b(tsinghua university|tsinghua|清华)\b", re.I), "清华"),
    (re.compile(r"\b(peking university|pku|北京大学|北大)\b", re.I), "北大"),
    (re.compile(r"\b(shanghai ai lab|shanghai artificial intelligence laboratory|上海人工智能实验室)\b", re.I), "上海 AI Lab"),
    (re.compile(r"\b(byte|bytedance|tiktok|字节)\b", re.I), "字节"),
    (re.compile(r"\b(deepseek)\b", re.I), "DeepSeek"),
    (re.compile(r"\b(qwen|alibaba|tongyi|阿里)\b", re.I), "阿里"),
    (re.compile(r"\b(zhipu|glm)\b", re.I), "智谱"),
    (re.compile(r"\b(moonshot|kimi)\b", re.I), "Moonshot"),
]


def detect_labs(paper: Paper) -> list[str]:
    """Return a deduped list of lab labels matched on this paper."""
    hay_parts: list[str] = []
    hay_parts.extend(a.affiliation for a in paper.authors if a.affiliation)
    hay_parts.append(paper.abstract or "")
    # First few authors' names + paper title sometimes contain affiliations
    # (rare but happens on arXiv).
    hay = "\n".join(hay_parts).lower()
    labs: list[str] = []
    for rx, label in LAB_PATTERNS:
        if rx.search(hay) and label not in labs:
            labs.append(label)
    return labs


def lab_badges(labs: Iterable[str]) -> list[dict[str, str]]:
    return [{"kind": "lab", "label": f"⭐ {l}"} for l in labs]
