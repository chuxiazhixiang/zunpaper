"""Top-level orchestrator: fetch -> render -> translate -> write feed JSON."""
from __future__ import annotations

import json
import logging
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from .digest import write_markdown_digest, write_rss
from .labs import detect_labs, lab_badges
from .scoring import score_paper
from .models import Paper, load_paper, save_paper
from .render import fetch_and_render
from .sources import arxiv_source
from .sources import hf_daily as hf_daily_source
from .sources import semantic_scholar as ss_source
from .sources import cn_news
from .sources import manual_xhs as manual_xhs_source
from .sources import manual_arxiv as manual_arxiv_source
from .translate import translate_with_retry

log = logging.getLogger(__name__)


def _existing_papers() -> dict[str, Paper]:
    out: dict[str, Paper] = {}
    if not cfg.PAPERS_DIR.exists():
        return out
    for p in cfg.PAPERS_DIR.glob("*.json"):
        try:
            out[p.stem] = load_paper(p)
        except Exception as e:
            log.warning("failed to load %s: %s", p, e)
    return out


def _is_translated(p: Paper) -> bool:
    """We treat a paper as fully translated only when every field the UI
    depends on is present. Missing cover_zh (added later in the project)
    forces a re-translate so old papers pick up the new Xiaohongshu-style
    headline on the next CI run."""
    return bool(p.abstract_zh and p.title_zh and p.cover_zh)


def process_new_papers(
    fresh: dict[str, Paper],
    existing: dict[str, Paper],
    enrich_ctx: "EnrichmentContext | None" = None,
) -> list[Paper]:
    """For each fetched paper: merge with cache, render cover, translate, and
    attach enrichment badges / related links from Phase 2 sources."""
    processed: list[Paper] = []
    for pid, paper in fresh.items():
        cached = existing.get(pid)
        if cached:
            # Merge channel union, keep cached translations and cover.
            for c in paper.channels:
                if c not in cached.channels:
                    cached.channels.append(c)
            if paper.updated and paper.updated > (cached.updated or ""):
                cached.updated = paper.updated
            paper = cached

        if not paper.cover_image and paper.pdf_url:
            rel, pages = fetch_and_render(paper.pdf_url, paper.id, cfg.COVER_DIR)
            if rel:
                paper.cover_image = rel
                if pages > 0:
                    paper.page_count = pages
                log.info("cover ready: %s (%d pages)", paper.id, pages)

        if not _is_translated(paper):
            t = translate_with_retry(paper.title, paper.abstract)
            paper.title_zh = t.title_zh or paper.title_zh or paper.title
            paper.abstract_zh = t.abstract_zh or paper.abstract_zh or paper.abstract
            paper.tldr_zh = t.tldr_zh or paper.tldr_zh
            paper.cover_zh = t.cover_zh or paper.cover_zh or paper.tldr_zh
            log.info("translated: %s", paper.id)

        if enrich_ctx is not None:
            enrich_ctx.apply(paper)

        save_paper(paper, cfg.PAPERS_DIR)
        processed.append(paper)

    return processed


@dataclass
class EnrichmentContext:
    """Bundle Phase 2 metadata so it can be applied uniformly to each paper."""

    hf_index: dict = field(default_factory=dict)
    ss_index: dict = field(default_factory=dict)
    news_index: dict = field(default_factory=dict)
    fresh_threshold_hours: int = 36

    def apply(self, paper: Paper) -> None:
        # Recompute all badges. Labs come from heuristic detection so they're
        # cheap to recompute.
        paper.badges = list(lab_badges(detect_labs(paper)))
        paper.related_links = list(paper.related_links or [])
        paper.source_tags = list(paper.source_tags or [])

        # 📌 Pinned-by-owner badge — fires for manual_arxiv source OR papers
        # that already have the `manual_pin` source_tag (so a paper pinned
        # once stays pinned even after the YAML entry is deleted).
        if (paper.source or "").lower() == "manual_arxiv" or "manual_pin" in paper.source_tags:
            if "manual_pin" not in paper.source_tags:
                paper.source_tags.append("manual_pin")
            paper.badges.append({"kind": "pin", "label": "📌 站长精选"})

        aid = paper.arxiv_id

        hf = self.hf_index.get(aid) if aid else None
        if hf:
            paper.badges.append({
                "kind": "hot",
                "label": f"🔥 HF · {hf.upvotes} 赞",
            })
            if "hf_daily" not in paper.source_tags:
                paper.source_tags.append("hf_daily")

        ss = self.ss_index.get(aid) if aid else None
        if ss and ss.citation_count >= 20:
            paper.badges.append({
                "kind": "hot",
                "label": f"⭐ 引用 {ss.citation_count}",
            })

        # Fresh badge: published within the lookback window
        if paper.published:
            try:
                pub = dt.datetime.fromisoformat(paper.published)
                hrs = (dt.datetime.now() - pub).total_seconds() / 3600
                if 0 <= hrs <= self.fresh_threshold_hours:
                    paper.badges.append({"kind": "fresh", "label": "⚡ 新鲜出炉"})
            except ValueError:
                pass

        # News articles
        if aid:
            seen_urls = {link.get("url") for link in paper.related_links}
            for art in self.news_index.get(aid, []):
                if art.url in seen_urls:
                    continue
                paper.related_links.append({
                    "source": art.source,
                    "source_name": art.source_name,
                    "title": art.title,
                    "url": art.url,
                })

        # Compute "为啥今天选了它" — fires for every paper, with or without
        # arxiv id. Runs last so HF / lab badges are already attached.
        paper.score, paper.score_breakdown = score_paper(paper)


def write_feed(all_papers: list[Paper]) -> None:
    """Write the master index, per-day digest, and channel list files."""
    cfg.ensure_dirs()
    all_papers_sorted = sorted(
        all_papers,
        key=lambda p: (p.published, p.id),
        reverse=True,
    )

    index_entries = [_feed_entry(p) for p in all_papers_sorted]
    with (cfg.DATA_DIR / "index.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "count": len(index_entries),
                "papers": index_entries,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # channels.json — for the frontend tabs.
    channels = cfg.load_channels()
    with (cfg.DATA_DIR / "channels.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "channels": [
                    {"id": c.id, "name": c.name, "emoji": c.emoji}
                    for c in channels
                ],
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # site.json — site-wide settings the frontend may want (title, colors).
    site = cfg.load_site()
    with (cfg.DATA_DIR / "site.json").open("w", encoding="utf-8") as f:
        json.dump(
            {
                "title": site.title,
                "subtitle": site.subtitle,
                "author": site.author,
                "primary_color": site.primary_color,
                "accent_color": site.accent_color,
                "feed_page_size": site.feed_page_size,
                "default_channel": site.default_channel,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Per-day file: papers published on that date.
    by_day: dict[str, list[Paper]] = {}
    for p in all_papers_sorted:
        if not p.published:
            continue
        by_day.setdefault(p.published, []).append(p)
    for day, items in by_day.items():
        with (cfg.DAILY_DIR / f"{day}.json").open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": day,
                    "count": len(items),
                    "papers": [_feed_entry(p) for p in items],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    # Days listing for archive page navigation.
    days_sorted = sorted(by_day.keys(), reverse=True)
    with (cfg.DATA_DIR / "days.json").open("w", encoding="utf-8") as f:
        json.dump({"days": days_sorted}, f, ensure_ascii=False, indent=2)


def _feed_entry(p: Paper) -> dict:
    """Slim representation used in feed JSON to keep it lightweight."""
    return {
        "id": p.id,
        "source": p.source,
        "title": p.title,
        "title_zh": p.title_zh or p.title,
        "tldr_zh": p.tldr_zh,
        "cover_zh": p.cover_zh or p.tldr_zh,
        "abstract_zh": p.abstract_zh,
        "authors": [a.name for a in p.authors[:3]],
        "primary_category": p.primary_category,
        "published": p.published,
        "channels": p.channels,
        "badges": p.badges,
        "cover_image": p.cover_image,
        "arxiv_id": p.arxiv_id,
        "abs_url": p.abs_url,
        "pdf_url": p.pdf_url,
        "score": p.score,
    }


def _build_enrichment_context(sources: cfg.SourcesConfig, fresh: dict[str, Paper]) -> EnrichmentContext:
    """Pull Phase 2 metadata only if those sources are enabled."""
    ctx = EnrichmentContext()

    if sources.hf_daily_enabled:
        try:
            entries = hf_daily_source.fetch_recent(lookback_days=3)
            ctx.hf_index = hf_daily_source.build_index(entries)
        except Exception as e:
            log.warning("hf_daily enrichment failed: %s", e)

    if sources.semantic_scholar_enabled and fresh:
        try:
            arxiv_ids = [p.arxiv_id for p in fresh.values() if p.arxiv_id]
            ctx.ss_index = ss_source.fetch_batch(arxiv_ids)
        except Exception as e:
            log.warning("semantic_scholar enrichment failed: %s", e)

    enabled = {
        "qbitai": sources.qbitai_enabled,
        "jiqizhixin": sources.jiqizhixin_enabled,
        "synced_review": sources.synced_review_enabled,
    }
    if any(enabled.values()):
        try:
            articles = cn_news.fetch_all_enabled(enabled)
            ctx.news_index = cn_news.build_arxiv_index(articles)
        except Exception as e:
            log.warning("cn_news enrichment failed: %s", e)

    return ctx


def _matches_channel(title: str, abstract: str, channel: cfg.Channel) -> bool:
    """Mirror of arxiv_source._matches_filters used for the channel-retag step."""
    text = f"{title}\n{abstract}".lower()
    if channel.exclude and any(kw.lower() in text for kw in channel.exclude):
        return False
    if not channel.keywords:
        return False  # don't auto-assign to keyword-less channels
    return any(kw.lower() in text for kw in channel.keywords)


def retag_and_prune(channels: list[cfg.Channel]) -> None:
    """Realign every cached paper with the CURRENT channels.yaml.

    Why: channels.yaml is the contract — when the owner changes it, the feed
    should reflect that on the next daily run. Papers that no longer match
    *any* current channel get deleted from disk (their cover image stays,
    cheap to leave). Manually-pinned papers are NEVER dropped; if they don't
    match any keyword we leave their existing channels alone.
    """
    if not cfg.PAPERS_DIR.exists():
        return
    valid_ch_ids = {c.id for c in channels}
    kept = 0
    dropped: list[str] = []
    for p in cfg.PAPERS_DIR.glob("*.json"):
        try:
            paper = load_paper(p)
        except Exception:
            continue

        pinned = (paper.source or "").lower() in ("manual_arxiv", "manual_xhs") \
                 or "manual_pin" in (paper.source_tags or [])

        matches = [c.id for c in channels if _matches_channel(paper.title, paper.abstract, c)]

        if matches:
            if set(paper.channels) != set(matches):
                paper.channels = matches
                save_paper(paper, cfg.PAPERS_DIR)
            kept += 1
        elif pinned:
            # Keep but trim to channels that still exist in config.
            paper.channels = [c for c in (paper.channels or []) if c in valid_ch_ids] or [channels[0].id]
            save_paper(paper, cfg.PAPERS_DIR)
            kept += 1
        else:
            try:
                p.unlink()
                dropped.append(paper.id)
            except OSError:
                pass

    log.info("retag_and_prune: kept %d, dropped %d (%s)", kept, len(dropped), ", ".join(dropped[:5]))


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg.ensure_dirs()
    channels = cfg.load_channels()
    sources = cfg.load_sources()

    # 1) Realign existing cached papers with the current channels.yaml BEFORE
    #    fetching anything. Off-topic papers are pruned so the feed stays
    #    aligned with what the owner currently cares about.
    retag_and_prune(channels)

    fresh: dict[str, Paper] = {}
    if sources.arxiv_enabled:
        fresh.update(arxiv_source.fetch_all(channels, sources))

    if sources.manual_xhs_enabled:
        for p in manual_xhs_source.load_posts():
            fresh[p.id] = p

    if sources.manual_arxiv_enabled:
        for p in manual_arxiv_source.load_papers(channels):
            # Manual paper wins over an arxiv re-fetch (so the owner can
            # override channels), but if both sources have the same id we
            # keep the manual-tagged copy.
            fresh[p.id] = p

    log.info("fetched %d unique papers", len(fresh))

    ctx = _build_enrichment_context(sources, fresh)

    existing = _existing_papers()
    process_new_papers(fresh, existing, ctx)

    # Re-enrich existing papers too (so badges/news stay fresh even if the paper
    # was fetched on an earlier day). Also back-fill translation fields the
    # current model expects (e.g. cover_zh was added later).
    all_papers = list(_existing_papers().values())
    for paper in all_papers:
        if paper.id in fresh:
            continue  # already enriched in process_new_papers

        if not _is_translated(paper):
            t = translate_with_retry(paper.title, paper.abstract)
            paper.title_zh = t.title_zh or paper.title_zh or paper.title
            paper.abstract_zh = t.abstract_zh or paper.abstract_zh or paper.abstract
            paper.tldr_zh = t.tldr_zh or paper.tldr_zh
            paper.cover_zh = t.cover_zh or paper.cover_zh or paper.tldr_zh
            log.info("back-fill translation: %s", paper.id)

        ctx.apply(paper)
        save_paper(paper, cfg.PAPERS_DIR)

    all_papers = list(_existing_papers().values())
    write_feed(all_papers)
    sorted_papers = sorted(all_papers, key=lambda p: (p.published, p.id), reverse=True)
    write_markdown_digest(sorted_papers)
    write_rss(sorted_papers)
    log.info("feed written: %d papers total", len(all_papers))


if __name__ == "__main__":
    run()
