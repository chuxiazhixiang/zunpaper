#!/usr/bin/env python3
"""把站内「💎 高质量」导出的 JSON 合并进 config/curated.yaml。

用法：
    python scripts/import_curated.py curated-export.json
    python scripts/import_curated.py curated-export.json --replace   # 用导出覆盖（默认是合并）

导出 JSON 形如：{"curated": ["arxiv-2606-12366", ...], "papers": [{"id":..,"title":..}]}
也接受纯 id 数组 ["arxiv-...", ...] 或 {"ids": [...]}。

合并后会保留 curated.yaml 里已有条目的 note。文件头部说明会原样重写保留。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from redpaper import config as cfg  # noqa: E402

CURATED_PATH = cfg.CONFIG_DIR / "curated.yaml"

HEADER = """\
# =================================================================
# 站长甄选 · 高质量论文「金标准」清单
# =================================================================
# 用途：
#   ① 站上给这些论文打「💎 站长甄选」徽章 + 评分加成（排序靠前、一眼可见）。
#   ② 每天 build 生成 site/data/curated.json —— 一份「站长口味」金标准数据集
#      （带标题/方向/机构/方法），后续用来反哺关键词召回、judge few-shot、
#      相似度推荐、量化 judge 准确率等，持续提升查找的准确率与质量。
#
# 怎么加：
#   - 直接在这里加 id（推荐 slug，如 arxiv-2606-12366；也接受 arxiv 号 2606.12366，
#     会自动归一化成 slug）。
#   - 或者在站内点卡片上的「💎」标记若干篇 → 用「导出高质量清单」下载 JSON →
#     跑 `python scripts/import_curated.py 下载的.json` 自动合并到这里，再提交。
#
# 这份清单本身就是「记录下来的论文名单」，跟着 git 走，永久留存。
# =================================================================
"""


def _read_existing() -> list[dict]:
    """返回 [{id, note}]（id 已归一化），保留顺序与 note。"""
    import yaml
    out: list[dict] = []
    if not CURATED_PATH.exists():
        return out
    raw = (yaml.safe_load(CURATED_PATH.read_text(encoding="utf-8")) or {}).get("curated") or []
    for item in raw:
        if isinstance(item, dict) and item.get("id"):
            out.append({"id": cfg._normalize_paper_id(item["id"]), "note": item.get("note", "")})
        elif isinstance(item, str) and item.strip():
            out.append({"id": cfg._normalize_paper_id(item), "note": ""})
    return out


def _read_export(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        ids = data
    elif isinstance(data, dict):
        ids = data.get("curated") or data.get("ids") or []
    else:
        ids = []
    return [cfg._normalize_paper_id(x) for x in ids if x]


def _write(entries: list[dict]) -> None:
    lines = [HEADER, "", "curated:"]
    if not entries:
        lines[-1] = "curated: []"
    for e in entries:
        lines.append(f"  - id: {e['id']}")
        if e.get("note"):
            note = str(e["note"]).replace('"', "'")
            lines.append(f'    note: "{note}"')
    CURATED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    replace = "--replace" in sys.argv
    if not args:
        print("用法: python scripts/import_curated.py curated-export.json [--replace]", file=sys.stderr)
        sys.exit(1)
    new_ids = _read_export(Path(args[0]))
    existing = [] if replace else _read_existing()
    seen = {e["id"] for e in existing}
    added = 0
    for pid in new_ids:
        if pid not in seen:
            existing.append({"id": pid, "note": ""})
            seen.add(pid)
            added += 1
    _write(existing)
    print(f"合并完成：新增 {added}，当前共 {len(existing)} 篇 → {CURATED_PATH}")
    print("记得 git add config/curated.yaml && commit，下次 build 就会打 💎 徽章 + 生成 curated.json")


if __name__ == "__main__":
    main()
