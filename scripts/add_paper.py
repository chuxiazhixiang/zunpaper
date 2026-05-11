#!/usr/bin/env python3
"""CLI helper: 一行命令把一篇 arxiv 论文加入「站长精选」清单。

用法：
    python scripts/add_paper.py https://arxiv.org/abs/2501.12345
    python scripts/add_paper.py 2501.12345 --note "RT-2 是开山之作"
    python scripts/add_paper.py 2501.12345 --channels manipulation,vla

这只更新 config/manual_arxiv.yaml；下次 GitHub Actions daily 跑就会自动把
论文拉下来上墙。本地也可以立刻：

    python scripts/build.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config" / "manual_arxiv.yaml"
ID_RE = re.compile(r"(\d{4}\.\d{4,5})(v\d+)?")


def normalise(raw: str) -> str | None:
    m = ID_RE.search(raw)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url_or_id", help="arxiv URL 或纯 ID")
    ap.add_argument("--note", default="", help="为啥精选这篇 (可选, 中文)")
    ap.add_argument(
        "--channels",
        default="",
        help="逗号分隔的频道 id (可选，缺省自动按关键词匹配)",
    )
    args = ap.parse_args()

    arxiv_id = normalise(args.url_or_id)
    if not arxiv_id:
        print(f"× 无法从 {args.url_or_id!r} 提取 arxiv id", file=sys.stderr)
        return 2

    data = yaml.safe_load(CFG.read_text(encoding="utf-8")) if CFG.exists() else {}
    if not isinstance(data, dict):
        data = {}
    papers = data.setdefault("papers", []) or []

    existing_ids: set[str] = set()
    for p in papers:
        if isinstance(p, str):
            nid = normalise(p)
            if nid:
                existing_ids.add(nid)
        elif isinstance(p, dict):
            nid = normalise(str(p.get("id") or p.get("url") or ""))
            if nid:
                existing_ids.add(nid)

    if arxiv_id in existing_ids:
        print(f"✓ {arxiv_id} 已经在精选列表里，跳过")
        return 0

    if args.note or args.channels:
        entry: dict = {"id": arxiv_id}
        if args.note:
            entry["note"] = args.note
        if args.channels:
            entry["channels"] = [c.strip() for c in args.channels.split(",") if c.strip()]
        papers.append(entry)
    else:
        papers.append(arxiv_id)

    data["papers"] = papers

    # Try to preserve top comments by re-writing only the YAML payload after them.
    text = CFG.read_text(encoding="utf-8") if CFG.exists() else ""
    head = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            head.append(line)
        else:
            break
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    CFG.write_text("\n".join(head + ["", body]).lstrip("\n"), encoding="utf-8")

    print(f"✓ 已添加 {arxiv_id} 到 {CFG.relative_to(ROOT)}")
    print("  下次 daily 会自动拉论文 + 翻译 + 上墙。")
    print("  本地立刻预览: python scripts/build.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
