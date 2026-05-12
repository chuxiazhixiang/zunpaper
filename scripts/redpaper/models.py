"""Core dataclasses for papers and feed entries."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Author:
    name: str
    affiliation: str = ""


@dataclass
class Paper:
    """A single paper across the pipeline.

    `id` is a stable slug used for filenames and URLs (e.g. arxiv-2501-12345).
    """

    id: str
    source: str                 # arxiv, hf_daily, manual_xhs, ...
    title: str
    title_zh: str = ""
    abstract: str = ""
    abstract_zh: str = ""
    tldr_zh: str = ""
    cover_zh: str = ""           # Xiaohongshu-style headline for the card cover
    authors: list[Author] = field(default_factory=list)
    primary_category: str = ""
    categories: list[str] = field(default_factory=list)
    published: str = ""         # ISO date YYYY-MM-DD
    updated: str = ""
    arxiv_id: str = ""
    pdf_url: str = ""
    abs_url: str = ""
    cover_image: str = ""       # site-relative path to first-page PNG
    preview_pages: list[str] = field(default_factory=list)  # extra PDF page jpgs (page 2..N)
    channels: list[str] = field(default_factory=list)
    badges: list[dict[str, str]] = field(default_factory=list)         # {kind, label}
    related_links: list[dict[str, str]] = field(default_factory=list)  # {source, source_name, title, url}
    page_count: int = 0
    source_tags: list[str] = field(default_factory=list)                # extra source markers (e.g. "hf_daily")
    score: int = 0
    score_breakdown: list[dict] = field(default_factory=list)            # [{id, label, points, hint}]
    # DeepSeek-V4-Flash judgment, set by build._judge_filter. None / {} = not judged yet
    # (legacy papers). UI can opt to surface `judge.reason` on detail page.
    judge: dict = field(default_factory=dict)                            # {relevant, research_value, primary_channel, reason, model}
    # DeepSeek-V4-Flash enrichment, set by enrich.enrich_paper. 用来在卡片下面
    # 展示「机构」和「方法 / 问题」二级标签。两个列表各 ≤3 项。
    institutions: list[str] = field(default_factory=list)                # ["MIT CSAIL", "Boston Dynamics", ...]
    method_tags: list[str] = field(default_factory=list)                 # ["DAgger", "VAE", "特技动作", ...]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["authors"] = [asdict(a) for a in self.authors]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Paper":
        authors = [Author(**a) for a in d.get("authors", [])]
        # Be forgiving: drop unknown keys so the model can evolve without
        # blowing up on older on-disk JSON.
        valid = {f.name for f in cls.__dataclass_fields__.values()} - {"authors"}
        clean = {k: v for k, v in d.items() if k in valid}
        return cls(authors=authors, **clean)


def save_paper(paper: Paper, dir_: Path) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{paper.id}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(paper.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def load_paper(path: Path) -> Paper:
    with path.open("r", encoding="utf-8") as f:
        return Paper.from_dict(json.load(f))
