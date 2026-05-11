"""One-off: re-translate English news papers that fell back to dryrun
during the main pipeline (Gemini per-minute rate limit). We pace the
calls (default 6s between requests = 10 RPM, comfortably under Gemini
free tier's 15 RPM) and write back to index.json + re-render the feed
so the homepage picks up proper Chinese titles.

Run from repo root:
    python scripts/retranslate_news.py [--limit N] [--pace 6.0]
"""
from __future__ import annotations
import argparse
import json
import re
import time
from pathlib import Path

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from redpaper.translate import translate_with_retry  # noqa: E402
from redpaper import config as cfg  # noqa: E402
from redpaper.models import Paper  # noqa: E402
from redpaper import build  # noqa: E402

EN_SOURCES = {"ieee_spectrum", "robohub", "therobotreport",
              "techcrunch_robotics", "synced_review"}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _needs_translation(p: dict) -> bool:
    if p.get("source") not in EN_SOURCES:
        return False
    tz = p.get("title_zh") or ""
    return not _CJK_RE.search(tz)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="cap how many papers to process; 0 = all")
    ap.add_argument("--pace", type=float, default=6.0,
                    help="seconds to sleep between LLM calls (default: 6.0)")
    args = ap.parse_args()

    idx_path = ROOT / "site" / "data" / "index.json"
    with open(idx_path, encoding="utf-8") as f:
        index = json.load(f)
    papers = index["papers"]

    targets = [p for p in papers if _needs_translation(p)]
    if args.limit:
        targets = targets[: args.limit]
    print(f"to translate: {len(targets)}")
    if not targets:
        return 0

    succeeded = failed = 0
    for i, p in enumerate(targets, 1):
        title = p.get("title") or ""
        abstract = p.get("abstract") or title
        print(f"[{i}/{len(targets)}] {p['id']}  «{title[:60]}»")
        try:
            t = translate_with_retry(title, abstract, retries=1, backoff=4.0)
        except Exception as e:
            print(f"  failed: {e}")
            failed += 1
            time.sleep(args.pace)
            continue
        if not _CJK_RE.search(t.title_zh or ""):
            print(f"  still no Chinese — backend exhausted?")
            failed += 1
            time.sleep(args.pace)
            continue
        p["title_zh"] = t.title_zh or p.get("title_zh") or title
        p["abstract_zh"] = t.abstract_zh or p.get("abstract_zh") or abstract
        p["tldr_zh"] = t.tldr_zh or p.get("tldr_zh") or ""
        p["cover_zh"] = t.cover_zh or p.get("cover_zh") or p["tldr_zh"]
        succeeded += 1
        print(f"  → {p['title_zh'][:60]}")
        # 周期写盘，避免中途崩了丢进度
        if i % 5 == 0:
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(index, f, ensure_ascii=False, indent=2)
        time.sleep(args.pace)

    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"\nDone: {succeeded} translated, {failed} failed")

    # 顺手把 site.json 也刷一下（lookback / generated_at），index 已经更新
    # 不需要 retag_and_prune，因为我们只改了翻译字段
    print("\nRe-rendering feed (digest + rss)…")
    paper_objs = [Paper.from_dict(d) for d in index["papers"]]
    build.write_feed(paper_objs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
