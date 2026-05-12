"""One-off: run DeepSeek-V4-Flash judge over every paper currently on the
site, write a drop-list report to tmp/judge-drops.md, and (with --apply)
physically remove the rejected ones.

Why a separate script: the main pipeline's gate only fires on NEW papers.
For the 58 already-on-site papers (most of which were crawled before we
had the judge), we need a back-fill pass.

Usage:
    python scripts/audit_judge.py            # dry-run report only
    python scripts/audit_judge.py --apply    # also delete the bad ones

Cost: ~58 papers × 0.5K tokens ≈ ¥0.07 single shot.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from redpaper import config as cfg  # noqa: E402
from redpaper.judge import judge_paper, JudgeCache, JudgeUnavailable  # noqa: E402

TMP_DIR = ROOT / "tmp"
DROPS_MD = TMP_DIR / "judge-drops.md"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete the rejected papers (default: dry run)")
    ap.add_argument("--pace", type=float, default=1.2,
                    help="sec between API calls (default 1.2 = 50 RPM, safe)")
    ap.add_argument("--limit", type=int, default=0, help="cap on # of judges")
    args = ap.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("ERROR: DEEPSEEK_API_KEY not set", file=sys.stderr)
        return 1

    cache = JudgeCache(ROOT / "data" / "judge_cache.json")
    idx_path = cfg.DATA_DIR / "index.json"
    with open(idx_path, encoding="utf-8") as f:
        idx = json.load(f)
    papers = idx["papers"]
    # Stable order: published desc so we judge newest first
    papers.sort(key=lambda p: (p.get("published") or "", p.get("id") or ""), reverse=True)

    keeps: list[dict] = []
    drops: list[tuple[dict, "Judgment"]] = []
    bypassed: list[str] = []
    n_called = n_cached = 0
    for i, p in enumerate(papers, 1):
        pid = p["id"]
        # Pinned / manual always kept
        if "manual_pin" in (p.get("source_tags") or []) or p.get("source") == "manual_arxiv":
            bypassed.append(pid)
            keeps.append(p)
            continue
        if args.limit and i > args.limit:
            keeps.append(p)
            continue

        cached = cache.get(pid)
        if cached is not None:
            n_cached += 1
            j = cached
        else:
            print(f"[{i:>2}/{len(papers)}] judge: {pid[:30]:<30} «{(p.get('title_zh') or p.get('title') or '')[:55]}»")
            try:
                j = judge_paper(p.get("title") or "", p.get("abstract") or p.get("title") or "")
            except JudgeUnavailable as e:
                print(f"  judge unavailable: {e}; skipping audit")
                return 1
            except Exception as e:
                print(f"  call failed: {e}; keep paper as safety")
                keeps.append(p)
                time.sleep(args.pace)
                continue
            cache.put(pid, j)
            n_called += 1
            time.sleep(args.pace)
        # 把 judgment 写回 paper 字段（即使最终留下来也带上）
        p["judge"] = {
            "relevant": j.relevant,
            "research_value": j.research_value,
            "primary_channel": j.primary_channel,
            "reason": j.reason,
            "model": j.model,
        }
        if j.relevant:
            keeps.append(p)
        else:
            drops.append((p, j))

    cache.save()

    # ---------- Write drop report ----------
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# DeepSeek-V4-Flash judge drop list",
        "",
        f"- 审计时间：{date.today().isoformat()}",
        f"- 候选 paper 数：**{len(papers)}**（其中 manual_pin / manual_arxiv 绕过门禁：{len(bypassed)}）",
        f"- 通过：**{len(keeps)}**",
        f"- 被砍：**{len(drops)}**",
        f"- 调用 DeepSeek 次数：{n_called}；缓存命中：{n_cached}",
        f"- 模式：{'**APPLY** — 将删除被砍 paper' if args.apply else '*DRY RUN* — 只输出报告，不动文件'}",
        "",
        "## 被砍清单",
        "",
        "| ID | 来源 | 标题 | 评分 | 价值 | 砍掉原因 |",
        "|---|---|---|---|---|---|",
    ]
    for p, j in drops:
        title = (p.get("title_zh") or p.get("title") or "").replace("|", "／")[:70]
        lines.append(
            f"| `{p['id']}` | {p.get('source','')} | {title} | {p.get('score',0)} | {j.research_value} | {j.reason.replace('|','／')[:120]} |"
        )
    if not drops:
        lines.append("| — | — | *全部通过，没有要砍的* | — | — | — |")
    lines.append("")
    lines.append("## 通过但 research_value=low 的（可手动复核）")
    lines.append("")
    lines.append("| ID | 标题 | 砍/留 reason |")
    lines.append("|---|---|---|")
    for p in keeps:
        j = p.get("judge") or {}
        if j.get("research_value") == "low":
            title = (p.get("title_zh") or p.get("title") or "").replace("|", "／")[:70]
            lines.append(f"| `{p['id']}` | {title} | {j.get('reason','')[:120]} |")
    DROPS_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n→ report: {DROPS_MD}")

    if args.apply and drops:
        # 1) 把 index.json 缩成只剩 keeps
        idx["papers"] = keeps
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        # 2) 删 papers/<id>.json 单文件
        # 3) 删 covers/<id>*.jpg
        from glob import glob
        n_paper_files = n_cover_files = 0
        for p, _ in drops:
            pj = cfg.PAPERS_DIR / f"{p['id']}.json"
            if pj.exists():
                pj.unlink()
                n_paper_files += 1
            for f in glob(str(cfg.COVER_DIR / f"{p['id']}*.jpg")):
                Path(f).unlink()
                n_cover_files += 1
        # 4) 重新生成 site.json + digest + rss + feed
        print(f"deleted {n_paper_files} paper json + {n_cover_files} cover jpg")
        # 5) 重新走 build.write_feed 重写 feed/digest/rss
        # 注意：index.json 里 authors 是字符串列表（前端用的扁平形式），
        # 而 papers/<id>.json 才是完整 Author 对象。要重建 feed 必须从
        # papers/*.json 加载，不能从 index.json 反序列化。
        from redpaper import build
        from redpaper.models import load_paper
        from redpaper.digest import write_markdown_digest, write_rss
        paper_objs = [load_paper(p) for p in sorted(cfg.PAPERS_DIR.glob("*.json"))]
        build.write_feed(paper_objs)
        sorted_p = sorted(paper_objs, key=lambda p: (p.published, p.id), reverse=True)
        write_markdown_digest(sorted_p)
        write_rss(sorted_p)
        print("re-rendered feed.json + digest + rss")

    print(f"keeps={len(keeps)} drops={len(drops)} called={n_called} cached={n_cached}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
