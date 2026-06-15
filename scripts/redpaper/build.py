"""Top-level orchestrator: fetch -> render -> translate -> write feed JSON."""
from __future__ import annotations

import json
import logging
import re
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from .digest import write_markdown_digest, write_rss
from .judge import judge_paper, judge_repo, JudgeUnavailable, JudgeCache
from .enrich import enrich_paper, EnrichUnavailable, EnrichCache
from .videos import VideoCache, enrich_paper_videos
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


_CJK_RE = __import__("re").compile(r"[\u4e00-\u9fff]")


def _judge_filter(fresh: dict[str, Paper]) -> dict[str, Paper]:
    """Run each new paper through the DeepSeek judge. Drop the irrelevant
    ones. Pinned manual papers always pass.

    Side effect: persists a `paper.judge` field on each kept paper so the
    UI can show the LLM's reasoning later if we want. Cache is keyed by
    paper id so re-runs don't repay for already-judged papers.
    """
    # 缓存放在仓库根目录 data/ 下，跟着 git 走，不会被 GitHub Pages 暴露
    cache = JudgeCache(cfg.REPO_ROOT / "data" / "judge_cache.json")
    kept: dict[str, Paper] = {}
    skipped_irrelevant = 0
    judge_calls = judge_cache_hits = 0
    for pid, p in fresh.items():
        # Pinned / manually-curated papers bypass the gate entirely
        if "manual_pin" in (p.source_tags or []) or p.source == "manual_arxiv":
            kept[pid] = p
            continue
        # GitHub 开源仓已经在 _process_github_repos 里走过 judge_repo，这里直接放行。
        if p.source == "github":
            kept[pid] = p
            continue

        cached = cache.get(pid)
        if cached is not None:
            judge_cache_hits += 1
            j = cached
        else:
            try:
                j = judge_paper(p.title, p.abstract or p.tldr_zh or p.title)
                judge_calls += 1
                cache.put(pid, j)
            except JudgeUnavailable as e:
                # No API key / explicitly disabled — pass through (don't
                # block the pipeline on a config issue).
                log.warning("judge skipped (%s); keeping %s without check", e, pid)
                kept[pid] = p
                continue
            except Exception as e:
                # Transient API hiccup — keep the paper to avoid data loss.
                log.warning("judge call failed for %s: %s; keeping", pid, e)
                kept[pid] = p
                continue

        p.judge = {
            "relevant": j.relevant,
            "research_value": j.research_value,
            "primary_channel": j.primary_channel,
            "reason": j.reason,
            "model": j.model,
        }
        if not j.relevant:
            skipped_irrelevant += 1
            log.info("judge[skip] %s «%s» → %s", pid, p.title[:50], j.reason[:80])
            continue
        kept[pid] = p

    cache.save()
    log.info("judge: %d called, %d cached, %d dropped as irrelevant",
             judge_calls, judge_cache_hits, skipped_irrelevant)
    return kept


def _enrich_papers(fresh: dict[str, Paper]) -> dict[str, Paper]:
    """Add `institutions` + `method_tags` chips to each paper via DeepSeek.

    Called after `_judge_filter`. Pinned manual papers go through enrich too
    (we still want their institution / method chips to show up). Cache the
    result so we don't re-pay on every build.
    """
    cache = EnrichCache(cfg.REPO_ROOT / "data" / "enrich_cache.json")
    enrich_calls = enrich_cache_hits = enrich_refresh = 0
    for pid, p in fresh.items():
        # GitHub 开源仓不抽机构/方法 chip（它们展示 star/语言/topics），跳过 enrich。
        if p.source == "github":
            continue
        cached = cache.get(pid)
        # 旧 cache 只有 institutions+method_tags，没有 platform/sim_stack 等 P1
        # 字段——需要重新调一次让 LLM 把新字段补齐。
        if cached is not None and cache.has_deep_fields(pid):
            enrich_cache_hits += 1
            _apply_enrichment(p, cached)
            continue
        if cached is not None:
            enrich_refresh += 1
        try:
            authors_text = "、".join(a.name for a in (p.authors or [])[:8])
            e = enrich_paper(p.title, p.abstract or p.tldr_zh or p.title, authors_text)
            enrich_calls += 1
            cache.put(pid, e)
            _apply_enrichment(p, e)
        except EnrichUnavailable as ex:
            log.warning("enrich skipped (%s); leaving %s without chips", ex, pid)
            # 即使 LLM 不可用，老 cache 里的两个字段也别丢
            if cached is not None:
                _apply_enrichment(p, cached)
        except Exception as ex:
            log.warning("enrich call failed for %s: %s", pid, ex)
            if cached is not None:
                _apply_enrichment(p, cached)
    cache.save()
    log.info(
        "enrich: %d called (incl. %d schema-refresh), %d cached",
        enrich_calls, enrich_refresh, enrich_cache_hits,
    )
    return fresh


def _apply_enrichment(p: Paper, e) -> None:
    """把 Enrichment 对象写回 Paper（避免到处重复 7 行赋值）。"""
    p.institutions = list(e.institutions or [])
    p.method_tags = list(e.method_tags or [])
    p.platform = list(getattr(e, "platform", None) or [])
    p.sim_stack = list(getattr(e, "sim_stack", None) or [])
    p.method_family = getattr(e, "method_family", "") or ""
    p.real_robot = getattr(e, "real_robot", "") or ""
    p.training_summary = getattr(e, "training_summary", "") or ""


def _scrape_demo_videos(fresh: dict[str, Paper]) -> None:
    """P0: 给每篇 paper 扫一次项目主页 / 摘要找 YouTube / Bilibili / mp4 demo。
    缓存在 `data/video_cache.json`，build pipeline 每天的增量只对没缓存过的
    paper 真正发 HTTP 请求。"""
    cache = VideoCache(cfg.REPO_ROOT / "data" / "video_cache.json")
    hits = misses = 0
    for pid, p in fresh.items():
        # GitHub 开源仓不扫 demo 视频（卡片不展示视频角标，扫了只是膨胀缓存）。
        if (p.source or "") == "github":
            continue
        try:
            videos = enrich_paper_videos(p, cache)
            p.demo_videos = videos
            if videos:
                hits += 1
            else:
                misses += 1
        except Exception as ex:
            log.warning("video extraction failed for %s: %s", pid, ex)
    cache.save()
    log.info("videos: %d papers got demo videos, %d papers had none", hits, misses)


def _github_should_fetch(refresh_days: int) -> bool:
    """节流：候选开源仓是慢变集合，没必要每天重新召回。距上次召回不足
    refresh_days 天、且盘上已有 github 卡时，跳过这轮抓取（卡片照样从盘上
    保留）。状态记在 data/github_state.json。"""
    if refresh_days <= 0:
        return True
    state_path = cfg.REPO_ROOT / "data" / "github_state.json"
    have_existing = any(cfg.PAPERS_DIR.glob("github-*.json"))
    if not have_existing:
        return True  # 首次：必须抓
    try:
        last = json.loads(state_path.read_text(encoding="utf-8")).get("last_fetch", "")
        last_dt = dt.datetime.fromisoformat(last)
        age_days = (dt.datetime.now(dt.timezone.utc) - last_dt).days
        return age_days >= refresh_days
    except Exception:
        return True


def _github_write_state(**fields) -> None:
    state_path = cfg.REPO_ROOT / "data" / "github_state.json"
    data = {}
    try:
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.update(fields)
    try:
        state_path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.warning("github state write failed: %s", e)


def _github_mark_fetched() -> None:
    # 成功：推进 last_fetch，进入 refresh_days 冷却。
    _github_write_state(last_fetch=dt.datetime.now(dt.timezone.utc).isoformat())


def _github_mark_attempt() -> None:
    # 失败：只记 last_attempt，不动 last_fetch —— 下次 build 仍可重试，不哑等一周。
    _github_write_state(last_attempt=dt.datetime.now(dt.timezone.utc).isoformat())


# 子方向 slug → 中文短标签（chip 展示用，不暴露内部 id）。
_GH_DIR_LABEL = {
    "loco-manip-wbc": "全身控制",
    "manipulation": "操作",
    "teleop": "遥操作",
    "locomotion": "运动控制",
    "world-model": "世界模型",
    "sim2real": "Sim2Real",
}


def _reconcile_github(active_ids: set[str]) -> None:
    """删除盘上不在本轮 kept 集合里的 github-*.json。

    用途：提高 min_stars / 改 judge prompt / 清 cache 重判后，旧的不达标 repo
    要从站点下架。只在一次 **成功** 的 refresh 之后调用（active_ids 可信），
    否则会误删全部。"""
    removed = 0
    for jp in cfg.PAPERS_DIR.glob("github-*.json"):
        if jp.stem not in active_ids:
            try:
                jp.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        log.info("github reconcile: removed %d de-listed repo(s)", removed)


def _process_github_repos(sources: cfg.SourcesConfig) -> tuple[dict[str, Paper], bool]:
    """召回候选开源仓 → judge_repo 过滤（缓存复用）→ 翻译描述 → 包成已就绪的
    Paper（source="github"，channels=["open-source"]）。

    返回 (papers, ok)。ok=True 表示「召回成功 + judge 可用」，此时调用方可以
    放心更新冷却时间戳、且本函数已对盘上旧 repo 做过 reconcile（下架不达标的）。
    ok=False（召回失败 / judge 不可用）时返回空 dict 且不动盘上已有卡片。"""
    from .sources import github_repos as gh_src

    out: dict[str, Paper] = {}
    try:
        repos = gh_src.fetch_candidate_repos(
            min_stars=sources.github_min_stars,
            max_repos=sources.github_max_repos,
        )
    except Exception as e:
        log.warning("github fetch failed: %s", e)
        return out, False
    if not repos:
        # 一次都没召回到（限流 / 网络） → 视为失败，不进冷却、不 reconcile。
        log.warning("github: 0 candidates fetched (treating as failure)")
        return out, False

    cache = JudgeCache(cfg.REPO_ROOT / "data" / "judge_cache.json")
    judged = kept = dropped = cache_hits = 0
    for d in repos:
        paper = gh_src.repo_to_paper(d)
        pid = paper.id
        j = cache.get(pid)
        if j is not None:
            cache_hits += 1
        else:
            try:
                topics = ", ".join(d.get("topics", []))
                j = judge_repo(d["full_name"], d.get("description", ""),
                               d.get("readme", ""), topics)
                judged += 1
                cache.put(pid, j)
            except JudgeUnavailable as e:
                # 没有 key / judge 被禁 —— 不能裸放行（会把课程/awesome/无关仓
                # 全塞进栏目）。整步放弃：返回空 + ok=False，保留盘上旧卡。
                cache.save()
                log.warning("github: repo judge unavailable (%s); skipping step "
                            "(no repos added, existing kept)", e)
                return {}, False
            except Exception as e:
                log.warning("repo judge failed for %s: %s; skipping", pid, e)
                continue
        paper.judge = {
            "relevant": j.relevant,
            "research_value": j.research_value,
            "primary_channel": j.primary_channel,
            "reason": j.reason,
            "model": j.model,
        }
        if not j.relevant:
            dropped += 1
            log.info("repo[skip] %s ⭐%s → %s", d["full_name"], d["stars"], j.reason[:60])
            continue
        # AI 判出的方向 → 既作为 channel（让二级方向标签能过滤开源项目），
        # 也作为中文 chip 展示在卡片上。
        if j.primary_channel and j.primary_channel != "none":
            paper.channels = [j.primary_channel]
            paper.method_tags = [_GH_DIR_LABEL.get(j.primary_channel, j.primary_channel)]
        out[pid] = paper
        kept += 1
    cache.save()

    # 翻译描述拿中文 headline（标题保留 owner/repo 原名）。已翻过的（盘上已有
    # 同 id）复用，这里只对新 repo 真正调用翻译后端。
    existing = _existing_papers()
    for pid, paper in out.items():
        prev = existing.get(pid)
        if prev is not None and (prev.cover_zh or prev.tldr_zh):
            paper.title_zh = prev.title_zh or paper.title
            paper.abstract_zh = prev.abstract_zh
            paper.tldr_zh = prev.tldr_zh
            paper.cover_zh = prev.cover_zh
            continue
        try:
            t = translate_with_retry(paper.title, paper.abstract)
            paper.abstract_zh = t.abstract_zh or paper.abstract
            paper.tldr_zh = t.tldr_zh or ""
            paper.cover_zh = t.cover_zh or t.tldr_zh or ""
        except Exception as e:
            log.warning("repo translate failed for %s: %s", pid, e)
        paper.title_zh = paper.title  # 保留 owner/repo 原名，不译
        if not paper.cover_zh:
            paper.cover_zh = (paper.abstract or paper.title)[:60]

    # reconcile：下架本轮不在 kept 集合里的旧 repo（min_stars 提高 / prompt 改 /
    # cache 清空重判后才能正确生效）。只在成功路径执行。
    _reconcile_github(set(out.keys()))

    log.info("github: %d kept, %d dropped, %d judged, %d cached", kept, dropped, judged, cache_hits)
    return out, True


def _is_translated(p: Paper) -> bool:
    """We treat a paper as fully translated only when every field the UI
    depends on is present *and* the title actually has Chinese characters
    in it. If the source title is English and translation fell back to
    dryrun (e.g. Gemini quota exhausted), title_zh stays English — those
    papers must be retried on the next CI run."""
    # GitHub 开源仓：标题保留 owner/repo 原名（不译），只要中文 headline
    # （cover_zh / tldr_zh）就位即视为已处理，避免每天重复翻译仓库名。
    if (p.source or "") == "github":
        return bool(p.cover_zh or p.tldr_zh)
    if not (p.abstract_zh and p.title_zh and p.cover_zh):
        return False
    src_title = p.title or ""
    src_has_zh = bool(_CJK_RE.search(src_title))
    tgt_has_zh = bool(_CJK_RE.search(p.title_zh))
    # 原标题非中文却没翻译出中文 → 视为没翻
    if not src_has_zh and not tgt_has_zh:
        return False
    return True


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
            # 同步回填 published：以前 cn_news 解析失败时 published 会是空串，
            # 之后修好 parser 后只更新 updated 又导致 published 永久为空。
            # 这里如果 cached.published 是空且 fresh paper 有 published，
            # 直接补上；如果两者都有，谁早听谁的（保留原始 first-publish 日期）。
            if paper.published:
                if not cached.published:
                    cached.published = paper.published
                elif paper.published < cached.published:
                    cached.published = paper.published
            # GitHub 开源仓的 star / 语言 / 归档 / topics / judge 每轮都会变，
            # 必须用本轮 fresh 覆盖回 cached，否则入库后这些字段永久陈旧、
            # open-source tab 的 star 排序也会 stale。翻译（cover_zh/tldr_zh）
            # 已在 _process_github_repos 里从旧卡继承到 fresh，这里不会丢。
            if (paper.source or "") == "github":
                if paper.github:
                    cached.github = paper.github
                if paper.judge:
                    cached.judge = paper.judge
                if paper.method_tags:
                    cached.method_tags = paper.method_tags
                if paper.abs_url:
                    cached.abs_url = paper.abs_url
                if paper.abstract:
                    cached.abstract = paper.abstract
                for fld in ("title_zh", "abstract_zh", "tldr_zh", "cover_zh"):
                    val = getattr(paper, fld, "")
                    if val:
                        setattr(cached, fld, val)
            paper = cached

        if not paper.cover_image and paper.pdf_url:
            rel, previews, pages = fetch_and_render(paper.pdf_url, paper.id, cfg.COVER_DIR)
            if rel:
                paper.cover_image = rel
                if previews:
                    paper.preview_pages = previews
                if pages > 0:
                    paper.page_count = pages
                log.info("cover ready: %s (%d pages, %d previews)", paper.id, pages, len(previews))
        elif paper.cover_image and not paper.preview_pages and paper.pdf_url:
            # Back-fill multi-page previews for papers that only had a cover
            # before (the multi-page renderer is new). Re-runs fetch_and_render
            # which is cache-aware: cover stays, only missing pages are rendered.
            rel, previews, pages = fetch_and_render(paper.pdf_url, paper.id, cfg.COVER_DIR)
            if previews:
                paper.preview_pages = previews
                log.info("backfilled previews: %s (+%d)", paper.id, len(previews))

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
    # 「⚡ 新鲜出炉」徽章窗口：3 天 = 72 小时。
    # 之前是 36h（1.5 天），刚过周末/工作日的 paper 在站点上常常一两天后
    # 就不带「新鲜」标签了，体感更新太快 → 拉长到 3 天，跟主流 paper feed
    # 的"最近 X 天"语义对齐。
    fresh_threshold_hours: int = 72

    def apply(self, paper: Paper) -> None:
        # Recompute all badges. Labs come from heuristic detection so they're
        # cheap to recompute.
        # GitHub 开源仓不跑 lab 徽章：README 里出现 MIT / Stanford 等字样会被
        # detect_labs 误判成「该实验室出品」。仓库卡展示 star/语言就够了。
        if (paper.source or "") == "github":
            paper.badges = []
        else:
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
    """Write the master index, per-day digest, and channel list files.

    The master index is now sorted by (score DESC, published DESC) so the
    homepage opens on the highest-quality papers regardless of when they
    landed. Per-day pages still sort by score within the day.
    """
    cfg.ensure_dirs()
    all_papers_sorted = sorted(
        all_papers,
        key=lambda p: (-(p.score or 0), p.published or "", p.id),
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
    sources_cfg = cfg.load_sources()
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
                # Crawl meta — let the homepage tell users which window
                # was scraped this run. Used by the "今日抓取" banner.
                "crawl_lookback_days": sources_cfg.arxiv_lookback_days,
                "crawl_evergreen_fallback_days": sources_cfg.arxiv_evergreen_fallback_days,
                "crawl_generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Per-day file: papers published on that date. Within a day we also
    # rank by score so the daily digest is "best first" too.
    by_day: dict[str, list[Paper]] = {}
    for p in all_papers:
        if not p.published:
            continue
        by_day.setdefault(p.published, []).append(p)

    # 清理「整天 paper 都被砍光」的 daily 文件 —— 否则 archive 页面会列出
    # 那一天，点进去 daily/<day>.json 还指着已经下架的 paper，进而 post.html
    # 报「论文不存在或还没拉取」。
    surviving_days = set(by_day.keys())
    for old in cfg.DAILY_DIR.glob("*.json"):
        if old.stem not in surviving_days:
            old.unlink()
            log.info("daily: removed stale %s.json (no surviving papers)", old.stem)

    for day, items in by_day.items():
        items.sort(key=lambda p: (-(p.score or 0), p.id))
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

    # 最后一步：给所有 HTML / JS 资源打上版本戳，绕开浏览器缓存。
    # 必须放在 write_feed 末尾才能确保部署时一定被执行。
    stamp_assets()


# ----- Asset cache-busting ------------------------------------------------

_ASSET_TAG_RE = __import__("re").compile(
    r'((?:src|href)\s*=\s*"assets/(?:js|css)/[^"?]+\.(?:js|css))(?:\?v=[^"]*)?(?=")'
)
_JS_IMPORT_RE = __import__("re").compile(
    r"((?:from|import)\s+['\"]\./[^'\"]+\.js)(?:\?v=[^'\"]*)?(?=['\"])"
)


def _compute_build_version() -> str:
    """Use the current git short SHA so each CI commit invalidates browser
    cache. Fallback to a timestamp for dirty / detached states (dev builds)."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=cfg.REPO_ROOT, stderr=subprocess.DEVNULL,
        ).decode().strip()
        if out:
            return out
    except Exception:
        pass
    return dt.datetime.utcnow().strftime("%Y%m%d%H%M%S")


def stamp_assets(version: str | None = None) -> str:
    """Append `?v=<version>` to every `<script src=...>` / `<link href=...>`
    in site/*.html, and to every `import './X.js'` / `from './X.js'` in
    site/assets/js/*.js. Idempotent — existing `?v=...` is stripped first.

    Why both HTML AND JS imports:
      - HTML 引用的 entry script (feed.js, post.js…) 被 stamp 后 URL 变化
        → 浏览器一定下载新版本。
      - 但新版本 feed.js 内部 `import './utils.js'` 如果不带 ?v=，浏览器会
        用 cache 里的老 utils.js → 缺 fetchJSON / BUILD_VERSION 等新逻辑。
        所以 JS-to-JS 的 import 也要 stamp。
    """
    v = version or _compute_build_version()
    log.info("stamping assets with version: %s", v)

    n_html = n_js = 0
    for path in cfg.SITE_DIR.glob("*.html"):
        src = path.read_text(encoding="utf-8")
        new = _ASSET_TAG_RE.sub(lambda m: f"{m.group(1)}?v={v}", src)
        if new != src:
            path.write_text(new, encoding="utf-8")
            n_html += 1

    for path in (cfg.SITE_DIR / "assets" / "js").glob("*.js"):
        src = path.read_text(encoding="utf-8")
        new = _JS_IMPORT_RE.sub(lambda m: f"{m.group(1)}?v={v}", src)
        if new != src:
            path.write_text(new, encoding="utf-8")
            n_js += 1

    log.info("stamped %d html + %d js files", n_html, n_js)
    return v


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
        "preview_pages": p.preview_pages,
        "arxiv_id": p.arxiv_id,
        "abs_url": p.abs_url,
        "pdf_url": p.pdf_url,
        "score": p.score,
        # DeepSeek-V4-Flash 的相关性 / 科研价值评论；前端可在详情页展示
        # `reason` 一行。空 dict 表示这条 paper 还没经过 judge（旧数据）。
        "judge": p.judge or {},
        # 二级 chip：机构 + 方法 / 问题 tag
        "institutions": p.institutions or [],
        "method_tags": p.method_tags or [],
        # P1: 领域专属结构化字段
        "platform": p.platform or [],
        "sim_stack": p.sim_stack or [],
        "method_family": p.method_family or "",
        "real_robot": p.real_robot or "",
        "training_summary": p.training_summary or "",
        # P0: demo 视频
        "demo_videos": p.demo_videos or [],
        # GitHub 开源项目元数据（source == "github" 时非空）
        "github": p.github or {},
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
        "embodied_techdaily": sources.embodied_techdaily_enabled,
        "shenlan_embodied": sources.shenlan_embodied_enabled,
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


_TITLE_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _normalize_title(title: str) -> str:
    """同一篇 qbitai 文章经常被挂到两个 URL（如 /412577 + /412870），slug
    哈希完全不同 → 当成两条独立 Paper。这里把标题归一化（去空白 / 标点 /
    符号 + 大小写折叠）作为二级去重 key。"""
    return _TITLE_NORM_RE.sub("", (title or "").lower())


def dedup_by_title(
    fresh: dict[str, Paper],
    existing: dict[str, Paper] | None = None,
) -> set[str]:
    """对 fresh 字典做标题级二级去重，返回被丢弃的 paper id 集合。
    保留策略：已存在缓存的优先，否则按 abs_url 字典序最小（qbitai URL 里
    有递增 ID，字典序小 ≈ 更早发布）。标题太短（<8 字符）不去重避免误伤。
    """
    existing = existing or {}
    by_title: dict[str, list[Paper]] = {}
    for p in fresh.values():
        k = _normalize_title(p.title)
        if len(k) < 8:
            continue
        by_title.setdefault(k, []).append(p)
    dropped: set[str] = set()
    for k, group in by_title.items():
        if len(group) < 2:
            continue
        cached_keepers = [p for p in group if p.id in existing]
        keepers = cached_keepers or group
        keeper = min(keepers, key=lambda p: (p.abs_url or "", p.id))
        for p in group:
            if p.id != keeper.id:
                dropped.add(p.id)
    for pid in dropped:
        fresh.pop(pid, None)
    if dropped:
        log.info("title-dedup: dropped %d duplicates (kept 1 each title)", len(dropped))
    return dropped


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

        # GitHub 开源仓不参与频道关键词匹配，也不能被 prune（否则每天 build
        # 开头就把上次抓的仓删光、白白重抓）。它们的方向频道来自 judge 判定，
        # 这里据 judge.primary_channel 同步 channels（兼带迁移老的 open-source）。
        if (paper.source or "").lower() == "github":
            pc = (paper.judge or {}).get("primary_channel", "")
            desired = [pc] if pc in valid_ch_ids else []
            if paper.channels != desired:
                paper.channels = desired
                save_paper(paper, cfg.PAPERS_DIR)
            kept += 1
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

    # 同标题去重：扫一遍盘上的 paper，标题完全相同的只留一份。
    by_title: dict[str, list[Paper]] = {}
    for p in cfg.PAPERS_DIR.glob("*.json"):
        try:
            paper = load_paper(p)
        except Exception:
            continue
        k = _normalize_title(paper.title)
        if len(k) < 8:
            continue
        by_title.setdefault(k, []).append(paper)
    title_dropped: list[str] = []
    for k, group in by_title.items():
        if len(group) < 2:
            continue
        # keep abs_url 字典序最小的（qbitai 数字 ID 小 ≈ 早发布）
        keeper = min(group, key=lambda x: (x.abs_url or "", x.id))
        for paper in group:
            if paper.id == keeper.id:
                continue
            try:
                (cfg.PAPERS_DIR / f"{paper.id}.json").unlink()
                title_dropped.append(paper.id)
            except OSError:
                pass
    if title_dropped:
        log.info(
            "retag_and_prune: title-dedup dropped %d (%s)",
            len(title_dropped),
            ", ".join(title_dropped[:5]),
        )


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

        # Evergreen 回补：今日窗口太薄就放宽 lookback 再扫一遍。
        # 不影响已经取到的论文（去重靠 dict key），只是把"过去 N 天"里
        # 当前关键词能命中的高分论文也补进来。新进的论文同样进打分流程，
        # 只有真的够分量的（高分 / 顶尖实验室）会浮到前面。
        threshold = sources.arxiv_evergreen_min_papers or 0
        if threshold > 0 and len(fresh) < threshold:
            fallback_days = sources.arxiv_evergreen_fallback_days
            log.info(
                "evergreen fallback: only %d papers in %d-day window, expanding to %d days",
                len(fresh), sources.arxiv_lookback_days, fallback_days,
            )
            from dataclasses import replace
            wider = replace(sources, arxiv_lookback_days=fallback_days)
            for pid, paper in arxiv_source.fetch_all(channels, wider).items():
                if pid not in fresh:
                    fresh[pid] = paper

    if sources.manual_xhs_enabled:
        for p in manual_xhs_source.load_posts():
            fresh[p.id] = p

    if sources.manual_arxiv_enabled:
        for p in manual_arxiv_source.load_papers(channels):
            # Manual paper wins over an arxiv re-fetch (so the owner can
            # override channels), but if both sources have the same id we
            # keep the manual-tagged copy.
            fresh[p.id] = p

    # 公众号 / 行业自媒体 — 没引用 arxiv 的高质量原创科普文章也作为
    # 独立卡片露出。中文已经是中文，跳过翻译；英文走 LLM 翻译。
    # 所有源都用 channels.yaml 关键词过滤，不相关方向直接丢。
    news_enabled = {
        "qbitai": sources.qbitai_enabled,
        "jiqizhixin": sources.jiqizhixin_enabled,
        "embodied_techdaily": sources.embodied_techdaily_enabled,
        "shenlan_embodied": sources.shenlan_embodied_enabled,
    }
    # 用 qbitai_lookback_days 作为统一的 news age 上限（它们在 sources.yaml 里
    # 都是同一个值；按需可以分源走，但目前先简化）。
    news_max_age = sources.qbitai_lookback_days or 60
    if any(news_enabled.values()):
        try:
            news_papers = cn_news.fetch_news_papers(
                news_enabled, channels,
                max_age_days=news_max_age,
                translate_en=True,
            )
            for p in news_papers:
                if p.id not in fresh:
                    fresh[p.id] = p
        except Exception as e:
            log.warning("cn_news standalone fetch failed: %s", e)

    # ----- P7: LLM 联网发现 -------------------------------------------
    # 走 Gemini-2.0/2.5 flash 带 google_search grounding 主动找最近 arxiv 论文，
    # 专门补 channels.yaml keyword 漏召回的（新工作命名 / 新平台名）。验证完
    # arxiv ID 真实存在再入站，防止 LLM 幻觉编 ID。
    if getattr(sources, "discover_enabled", True):
        try:
            from . import discover as _discover
            existing_ids = set(fresh.keys())
            for jp in cfg.PAPERS_DIR.glob("*.json"):
                existing_ids.add(jp.stem)
            disc_papers = _discover.discover_recent_papers(
                channels,
                existing_ids,
                days=getattr(sources, "discover_lookback_days", 14),
                per_channel=getattr(sources, "discover_per_channel", 5),
            )
            for p in disc_papers:
                if p.id not in fresh:
                    fresh[p.id] = p
            log.info("discover: %d added (post-validation)", len(disc_papers))
        except Exception as e:
            log.warning("discover step failed: %s", e)

    # ----- P5: 视频频道源（YouTube + Bilibili 厂商 demo） ---------------
    # 用 sources.video_channels_enabled 总开关。每条视频包成 Paper 卡，跟
    # cn_news 走同一个 score / enrich 流程。Bilibili API 偶尔风控，挂了不
    # 影响 YouTube 那几个稳定厂商频道。
    if getattr(sources, "video_channels_enabled", True):
        try:
            from .sources import video_channels as _video_channels
            video_papers = _video_channels.fetch_all_video_channels(
                limit_per_channel=getattr(sources, "video_per_channel", 6),
                max_age_days=getattr(sources, "video_lookback_days", 30),
            )
            for p in video_papers:
                if p.id not in fresh:
                    fresh[p.id] = p
        except Exception as e:
            log.warning("video channel fetch failed: %s", e)

    # ----- 开源项目栏目（GitHub repos） --------------------------------
    # 召回高 star 仓 → judge_repo 砍课程/复现/蹭名 → 包成 open-source 频道卡片。
    # 节流：refresh_days 天内已抓过就跳过（卡片从盘上保留），省 API + token。
    if getattr(sources, "github_enabled", True):
        if _github_should_fetch(getattr(sources, "github_refresh_days", 7)):
            try:
                gh_papers, gh_ok = _process_github_repos(sources)
                for pid, p in gh_papers.items():
                    fresh[pid] = p
                # 只有真正成功（召回到候选 + judge 可用）才进 7 天冷却；
                # 失败只记一次尝试，让下次 build 还能重试，而不是哑等一周。
                if gh_ok:
                    _github_mark_fetched()
                else:
                    _github_mark_attempt()
            except Exception as e:
                log.warning("github repos step failed: %s", e)
        else:
            log.info("github: within refresh window, skipping fetch (repos kept from disk)")

    log.info("fetched %d unique papers", len(fresh))

    # 标题级二级去重：qbitai 等公众号偶尔同篇内容挂多个 URL，slug 哈希不同
    # 但标题完全一样。fresh 里靠 URL 哈希去重不掉这种，在这里做一次清理。
    existing_pre = _existing_papers()
    dedup_by_title(fresh, existing_pre)

    # ----- LLM 质量门禁（DeepSeek V4-Flash judge）----------------------
    # 关键词命中 ≠ 真相关。在 paper 进入上站流水线之前，让 DeepSeek 判定
    # (1) 是否真的属于站长方向，(2) 是否有科研价值。relevant=False 的直接
    # 丢弃，并把判定缓存到 data/judge_cache.json，下一轮 build 不再重复付费。
    # 钉过 manual_pin 的（重要论文）跳过 judge。
    fresh = _judge_filter(fresh)
    log.info("after judge: %d papers kept", len(fresh))

    # ----- 二级标签抽取（机构 + 方法 / 问题 + P1 结构化字段） ----------
    # 在 judge 通过之后再 enrich，避免给被砍掉的 paper 浪费 token。
    _enrich_papers(fresh)

    # ----- P0: demo 视频抓取 --------------------------------------------
    # 摘要 + 项目主页扫一遍，命中 YouTube / Bilibili / mp4 直接缓存。
    _scrape_demo_videos(fresh)

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

    # ----- P6: monthly digest（当前月）-------------------------------------
    # 只重生成本月份，避免每天把所有月份都烧一遍 LLM。生成后写完 monthly_index。
    _refresh_current_month_digest(sorted_papers)


def _refresh_current_month_digest(all_papers: list[Paper]) -> None:
    """重生成当前月份的 LLM 综述（如果当月已有 ≥5 篇 paper）。"""
    try:
        from datetime import date
        from .monthly_digest import (
            MonthlyDigestUnavailable,
            generate_monthly_digest,
            write_digest_files,
            write_index,
            MonthlyDigest,
        )
    except Exception as e:
        log.warning("monthly_digest import failed: %s", e)
        return

    current_ym = date.today().strftime("%Y-%m")
    month_papers = [p for p in all_papers
                    if (p.published or "")[:7] == current_ym]
    if len(month_papers) < 5:
        log.info("monthly digest skipped: only %d papers in %s",
                 len(month_papers), current_ym)
        return
    try:
        d = generate_monthly_digest(current_ym, all_papers)
    except MonthlyDigestUnavailable as e:
        log.info("monthly digest unavailable: %s", e)
        return
    except Exception as e:
        log.warning("monthly digest generation failed: %s", e)
        return
    write_digest_files(d)
    log.info("monthly digest refreshed: %s (%d papers)", current_ym, d.paper_count)

    # 重写 monthly_index.json：扫盘上所有月份 json
    json_dir = cfg.REPO_ROOT / "site" / "data" / "digest" / "monthly"
    all_digests: list[MonthlyDigest] = []
    for fp in sorted(json_dir.glob("*.json")):
        try:
            j = json.loads(fp.read_text("utf-8"))
            all_digests.append(MonthlyDigest(
                year_month=j.get("year_month", ""),
                headline=j.get("headline", ""),
                summary_md=j.get("summary_md", ""),
                themes=j.get("themes") or [],
                paper_count=j.get("paper_count", 0),
                paper_ids=j.get("paper_ids") or [],
                model=j.get("model", ""),
                generated_at=j.get("generated_at", ""),
            ))
        except Exception:
            continue
    write_index(all_digests)


if __name__ == "__main__":
    run()
