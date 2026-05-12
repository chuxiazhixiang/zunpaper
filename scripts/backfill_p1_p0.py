"""One-off backfill for P1 (structured extraction) + P0 (demo videos).

逐篇过 `site/data/papers/*.json`：
  - 如果 enrich_cache 里没有 P1 新字段（platform/sim_stack/...），调一次 DeepSeek 回填。
  - 给每篇 paper 抓一次 demo 视频（项目主页 / 摘要扫 YouTube + Bilibili + mp4）。
  - 把更新后的 Paper 对象写回 `site/data/papers/*.json`。
  - 完成后重写 `feed.json` / `index.json` / `daily/*.json` / `rss.xml`，让前端立刻看到新字段。

Run:
    DEEPSEEK_API_KEY=sk-... python scripts/backfill_p1_p0.py            # dry-run
    DEEPSEEK_API_KEY=sk-... python scripts/backfill_p1_p0.py --apply

最大开销：~94 次 DeepSeek-V4-Flash + ~80 次 HTTP（项目主页有限）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from redpaper import build                                # noqa: E402
from redpaper import config as cfg                        # noqa: E402
from redpaper.digest import write_markdown_digest, write_rss  # noqa: E402
from redpaper.enrich import (                         # noqa: E402
    EnrichCache,
    EnrichUnavailable,
    enrich_paper,
)
from redpaper.models import Paper                     # noqa: E402
from redpaper.videos import VideoCache, enrich_paper_videos  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("backfill")


def _load_paper(p: Path) -> Paper:
    return Paper.from_dict(json.loads(p.read_text("utf-8")))


def _save_paper(paper: Paper, p: Path) -> None:
    from dataclasses import asdict
    p.write_text(
        json.dumps(asdict(paper), ensure_ascii=False, indent=2, default=str),
        "utf-8",
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写回 papers/*.json + 重生成 feed")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇（调试用）")
    ap.add_argument("--skip-enrich", action="store_true", help="只跑视频抓取，不调 LLM")
    ap.add_argument("--skip-videos", action="store_true", help="只跑 enrich，不抓视频")
    args = ap.parse_args()

    papers_dir = ROOT / "site" / "data" / "papers"
    files = sorted(papers_dir.glob("*.json"))
    if args.limit:
        files = files[: args.limit]
    log.info("loaded %d papers", len(files))

    enrich_cache = EnrichCache(ROOT / "data" / "enrich_cache.json")
    video_cache = VideoCache(ROOT / "data" / "video_cache.json")

    n_enrich_call = n_enrich_hit = n_enrich_fail = 0
    n_video_hit = n_video_miss = 0
    updated_papers: list[tuple[Path, Paper]] = []

    for i, fp in enumerate(files, 1):
        paper = _load_paper(fp)
        dirty = False

        # ----- P1: enrich ---------------------------------------------------
        if not args.skip_enrich:
            cached = enrich_cache.get(paper.id)
            need_call = cached is None or not enrich_cache.has_deep_fields(paper.id)
            if cached is not None and not need_call:
                n_enrich_hit += 1
                build._apply_enrichment(paper, cached)
                dirty = True
            elif need_call:
                try:
                    authors_text = "、".join(a.name for a in (paper.authors or [])[:8])
                    e = enrich_paper(
                        paper.title,
                        paper.abstract or paper.tldr_zh or paper.title,
                        authors_text,
                    )
                    enrich_cache.put(paper.id, e)
                    build._apply_enrichment(paper, e)
                    n_enrich_call += 1
                    dirty = True
                except EnrichUnavailable as ex:
                    log.warning("enrich unavailable for %s: %s", paper.id, ex)
                    n_enrich_fail += 1
                    if cached is not None:
                        # 老 cache 至少把 institutions / method_tags 写回
                        build._apply_enrichment(paper, cached)
                        dirty = True
                except Exception as ex:
                    log.warning("enrich failed for %s: %s", paper.id, ex)
                    n_enrich_fail += 1

        # ----- P0: videos ---------------------------------------------------
        if not args.skip_videos:
            try:
                videos = enrich_paper_videos(paper, video_cache)
                paper.demo_videos = videos
                if videos:
                    n_video_hit += 1
                else:
                    n_video_miss += 1
                dirty = True
            except Exception as ex:
                log.warning("video fetch failed for %s: %s", paper.id, ex)

        if dirty:
            updated_papers.append((fp, paper))

        if i % 10 == 0:
            log.info(
                "  progress %d/%d | enrich call=%d hit=%d fail=%d | video hit=%d miss=%d",
                i, len(files), n_enrich_call, n_enrich_hit, n_enrich_fail,
                n_video_hit, n_video_miss,
            )

        # 每 25 篇存一次 cache，防止中途崩溃丢进度
        if i % 25 == 0:
            enrich_cache.save()
            video_cache.save()

    enrich_cache.save()
    video_cache.save()

    log.info("---- summary ----")
    log.info("enrich: %d new calls / %d cache-hit / %d failed",
             n_enrich_call, n_enrich_hit, n_enrich_fail)
    log.info("videos: %d papers with videos / %d without", n_video_hit, n_video_miss)
    log.info("papers updated: %d", len(updated_papers))

    if not args.apply:
        log.info("dry-run; pass --apply to write papers/*.json + regenerate feed.")
        return

    # ----- 写回 papers/*.json ------------------------------------------------
    for fp, paper in updated_papers:
        _save_paper(paper, fp)

    # ----- 重生成 feed/index/daily/rss --------------------------------------
    log.info("regenerating feed.json / index.json / daily/*.json / rss ...")
    # 重新从盘上读所有 paper（含未改动的），保证 feed 全量一致
    all_papers = [_load_paper(fp) for fp in sorted(papers_dir.glob("*.json"))]
    build.write_feed(all_papers)
    sorted_papers = sorted(all_papers, key=lambda p: (p.published, p.id), reverse=True)
    write_markdown_digest(sorted_papers)
    write_rss(sorted_papers)
    log.info("done.")


if __name__ == "__main__":
    main()
