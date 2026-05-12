"""One-off: pull recent videos from configured YouTube + Bilibili channels and
add them as `vid-…` Paper cards on the homepage.

P5 的"立刻见效"版：直接抓视频频道最近 N 条视频，每条转成 Paper 卡，写入
`site/data/papers/vid-*.json`，再重新生成 feed。

Run:
    python scripts/ingest_video_channels.py                          # dry-run
    python scripts/ingest_video_channels.py --apply                  # actually write papers + feed
    python scripts/ingest_video_channels.py --apply --max-age 14     # 只要近 14 天的
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from redpaper import build                                      # noqa: E402
from redpaper import config as cfg                              # noqa: E402
from redpaper.digest import write_markdown_digest, write_rss    # noqa: E402
from redpaper.models import Paper                               # noqa: E402
from redpaper.scoring import score_paper                        # noqa: E402
from redpaper.sources.video_channels import fetch_all_video_channels  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("video_ingest")


def _load_paper(p: Path) -> Paper:
    return Paper.from_dict(json.loads(p.read_text("utf-8")))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入 papers/*.json + 重生成 feed")
    ap.add_argument("--limit-per-channel", type=int, default=6)
    ap.add_argument("--max-age", type=int, default=30,
                    help="只保留 N 天内的视频（默认 30）")
    args = ap.parse_args()

    videos = fetch_all_video_channels(
        limit_per_channel=args.limit_per_channel,
        max_age_days=args.max_age,
    )
    log.info("fetched %d video Paper cards", len(videos))

    if not args.apply:
        log.info("dry-run; pass --apply to actually write.")
        for v in videos[:20]:
            log.info("  %s | %s | %s", v.source, v.published, v.title[:55])
        return

    papers_dir = ROOT / "site" / "data" / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    written = updated = 0
    for v in videos:
        path = papers_dir / f"{v.id}.json"
        # Score：让 from_media 规则把视频排到首页中段，而不是默认 0 分垫底。
        score, breakdown = score_paper(v)
        v.score = score
        v.score_breakdown = breakdown
        if path.exists():
            # 已存在则刷新打分 + 视频元数据，不动 institutions 之类已 enrich 的字段
            existing = _load_paper(path)
            existing.score = score
            existing.score_breakdown = breakdown
            existing.demo_videos = v.demo_videos
            existing.title = v.title
            existing.title_zh = v.title
            path.write_text(
                json.dumps(asdict(existing), ensure_ascii=False, indent=2, default=str),
                "utf-8",
            )
            updated += 1
            continue
        path.write_text(
            json.dumps(asdict(v), ensure_ascii=False, indent=2, default=str),
            "utf-8",
        )
        written += 1
    log.info("wrote %d new video cards, refreshed %d existing", written, updated)

    log.info("regenerating feed / digest / rss ...")
    all_papers = [_load_paper(fp) for fp in sorted(papers_dir.glob("*.json"))]
    build.write_feed(all_papers)
    sorted_papers = sorted(all_papers, key=lambda p: (p.published, p.id), reverse=True)
    write_markdown_digest(sorted_papers)
    write_rss(sorted_papers)
    log.info("done.")


if __name__ == "__main__":
    main()
