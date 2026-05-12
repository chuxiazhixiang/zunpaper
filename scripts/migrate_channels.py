"""One-off: remap legacy channel IDs to the new 5-category scheme and
re-run keyword detection so old papers can pick up new channels (teleop /
sim2real) where applicable.

Old → new mapping:
    whole-body      → loco-manip-wbc
    loco-manip      → loco-manip-wbc
    vla             → loco-manip-wbc   (most VLA work is humanoid + WBC)
    manipulation    → manipulation     (unchanged)
    locomotion      → locomotion       (unchanged)

After the rename we also re-run keyword matching with the NEW channels.yaml
so a paper that mentions "teleoperation" or "sim-to-real" gets the
corresponding channel even if it was originally crawled under whole-body.

Run:
    python scripts/migrate_channels.py            # dry-run
    python scripts/migrate_channels.py --apply    # actually rewrite papers/*.json + feed
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import os
import time

from redpaper import config as cfg  # noqa: E402
from redpaper import build  # noqa: E402
from redpaper.models import save_paper, load_paper  # noqa: E402
from redpaper.digest import write_markdown_digest, write_rss  # noqa: E402
from redpaper.enrich import EnrichCache, enrich_paper, EnrichUnavailable  # noqa: E402


REMAP = {
    "whole-body": "loco-manip-wbc",
    "loco-manip": "loco-manip-wbc",
    "vla": "loco-manip-wbc",
    # 直通
    "manipulation": "manipulation",
    "locomotion": "locomotion",
    "teleop": "teleop",
    "sim2real": "sim2real",
    "loco-manip-wbc": "loco-manip-wbc",
}


def keyword_channels(text: str, channels) -> list[str]:
    """Re-run channels.yaml keyword matching to discover missed channels."""
    text_l = text.lower()
    out: list[str] = []
    for ch in channels:
        if ch.exclude and any(kw.lower() in text_l for kw in ch.exclude):
            continue
        if not ch.keywords:
            continue
        if any(kw.lower() in text_l for kw in ch.keywords):
            out.append(ch.id)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--skip-enrich", action="store_true",
                    help="don't back-fill institutions / method_tags chips")
    ap.add_argument("--pace", type=float, default=1.2)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("migrate_channels")

    channels = cfg.load_channels()
    valid_ids = {c.id for c in channels}
    log.info("new channel IDs: %s", sorted(valid_ids))

    changes: list[tuple[str, list[str], list[str]]] = []  # (id, old, new)
    files = sorted(cfg.PAPERS_DIR.glob("*.json"))
    for f in files:
        p = load_paper(f)
        old = list(p.channels or [])
        # 1) map legacy IDs
        new = []
        for c in old:
            mapped = REMAP.get(c, c)
            if mapped in valid_ids and mapped not in new:
                new.append(mapped)
        # 2) discover additional channels via keyword scan
        text = " ".join([p.title or "", p.abstract or "", p.title_zh or "",
                         p.abstract_zh or "", p.tldr_zh or ""])
        for kc in keyword_channels(text, channels):
            if kc not in new:
                new.append(kc)
        # 3) if still empty, fall back to the most popular one
        if not new:
            new = ["loco-manip-wbc"]
        if new != old:
            changes.append((p.id, old, new))
            p.channels = new
            if args.apply:
                save_paper(p, cfg.PAPERS_DIR)

    log.info("changes: %d / %d papers", len(changes), len(files))
    for pid, o, n in changes[:30]:
        log.info("  %s: %s → %s", pid[:40], o, n)
    if len(changes) > 30:
        log.info("  ... and %d more", len(changes) - 30)

    if args.apply:
        # 在重写 feed 之前补 chip：把没有 institutions / method_tags 的老 paper
        # 也送一遍 enrich，以便首页卡片立刻能看到二级标签。
        if not args.skip_enrich and os.environ.get("DEEPSEEK_API_KEY"):
            ecache = EnrichCache(ROOT / "data" / "enrich_cache.json")
            todo = []
            for f in files:
                p = load_paper(f)
                if not p.institutions and not p.method_tags:
                    todo.append(p)
            log.info("enrich back-fill: %d papers without chips", len(todo))
            for i, p in enumerate(todo, 1):
                cached = ecache.get(p.id)
                if cached is not None:
                    p.institutions = cached.institutions
                    p.method_tags = cached.method_tags
                    save_paper(p, cfg.PAPERS_DIR)
                    continue
                try:
                    authors_text = "、".join(a.name for a in (p.authors or [])[:8])
                    e = enrich_paper(p.title or p.title_zh or "",
                                     p.abstract or p.tldr_zh or "",
                                     authors_text)
                    ecache.put(p.id, e)
                    p.institutions = e.institutions
                    p.method_tags = e.method_tags
                    save_paper(p, cfg.PAPERS_DIR)
                    log.info("[%3d/%d] enrich %s → inst=%s methods=%s",
                             i, len(todo), p.id[:32], e.institutions, e.method_tags)
                    time.sleep(args.pace)
                except EnrichUnavailable as ex:
                    log.warning("enrich skipped (%s)", ex)
                    break
                except Exception as ex:
                    log.warning("enrich error on %s: %s", p.id, ex)
            ecache.save()

        log.info("rewriting feed / digest / rss ...")
        all_papers = list(build._existing_papers().values())
        build.write_feed(all_papers)
        sorted_p = sorted(all_papers, key=lambda p: (p.published, p.id), reverse=True)
        write_markdown_digest(sorted_p)
        write_rss(sorted_p)
        log.info("done — %d papers on site", len(all_papers))
    else:
        log.info("(dry run; pass --apply to commit)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
