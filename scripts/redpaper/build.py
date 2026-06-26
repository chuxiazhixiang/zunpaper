"""Top-level orchestrator: fetch -> render -> translate -> write feed JSON."""
from __future__ import annotations

import json
import logging
import os
import re
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from .digest import write_markdown_digest, write_rss
from .judge import (
    judge_paper, judge_repo, judge_paper_for_channel,
    channel_prompt_signature, JudgeUnavailable, JudgeCache, CustomChannelCache,
    expand_channel_keywords, keyword_expand_signature, KeywordCache,
)
from .enrich import enrich_paper, EnrichUnavailable, EnrichCache
from .videos import VideoCache, enrich_paper_videos
from .labs import detect_labs, lab_badges
from .scoring import score_paper
from .venues import parse_venue
from .models import Paper, Author, load_paper, save_paper
from .render import fetch_and_render, extract_head_text as fetch_head_text
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


def _custom_rescue(p: Paper, custom_channels: list[cfg.Channel],
                   ccache: "CustomChannelCache") -> bool:
    """核心 judge 砍掉一篇论文前，给自定义分类一次「捞回」机会：若该论文命中某个
    自定义频道的关键词、且用该频道**独立 prompt** 判定属于该方向，就保留它，并把
    该频道写入 paper.channels。返回是否被捞回。

    无 API key（dryrun）时退化为纯关键词命中即收（噪音多但零成本，符合「买不起
    token 也能用」的取向）。
    """
    rescued = False
    for c in custom_channels:
        if not _matches_channel(p.title, p.abstract, c):
            continue
        sig = channel_prompt_signature(c)
        verdict = ccache.get(p.id, c.id, sig)
        if verdict is None:
            try:
                jj = judge_paper_for_channel(p.title, p.abstract or p.tldr_zh or p.title, c)
                ccache.put(p.id, c.id, jj, sig)
                verdict = jj.relevant
            except JudgeUnavailable:
                verdict = True  # 无 token → 关键词命中即收
            except Exception as e:
                log.warning("custom judge failed for %s @ %s: %s", p.id, c.id, e)
                verdict = False
        if verdict:
            if c.id not in p.channels:
                p.channels.append(c.id)
            p.judge = {
                "relevant": True,
                "research_value": "medium",
                "primary_channel": c.id,
                "reason": f"自定义分类「{c.name}」收录",
                "model": "custom",
            }
            rescued = True
    return rescued


def _judge_filter(fresh: dict[str, Paper],
                  custom_channels: list[cfg.Channel] | None = None) -> dict[str, Paper]:
    """Run each new paper through the DeepSeek judge. Drop the irrelevant
    ones. Pinned manual papers always pass.

    Side effect: persists a `paper.judge` field on each kept paper so the
    UI can show the LLM's reasoning later if we want. Cache is keyed by
    paper id so re-runs don't repay for already-judged papers.

    custom_channels（config/channels.d 来的自定义分类）走「B 方案」：核心 judge
    判定不相关、但论文命中某自定义频道关键词且该频道独立 prompt 收下时，照样保留。
    """
    # 缓存放在仓库根目录 data/ 下，跟着 git 走，不会被 GitHub Pages 暴露
    cache = JudgeCache(cfg.REPO_ROOT / "data" / "judge_cache.json")
    custom_channels = custom_channels or []
    ccache = (CustomChannelCache(cfg.REPO_ROOT / "data" / "custom_judge_cache.json")
              if custom_channels else None)
    kept: dict[str, Paper] = {}
    skipped_irrelevant = 0
    rescued_count = 0
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
            # 核心方向不收，但自定义分类可能想要 → 给一次捞回机会。
            if custom_channels and _custom_rescue(p, custom_channels, ccache):
                rescued_count += 1
                kept[pid] = p
                continue
            skipped_irrelevant += 1
            log.info("judge[skip] %s «%s» → %s", pid, p.title[:50], j.reason[:80])
            continue
        kept[pid] = p

    cache.save()
    if ccache is not None:
        ccache.save()
    log.info("judge: %d called, %d cached, %d dropped, %d rescued by custom channels",
             judge_calls, judge_cache_hits, skipped_irrelevant, rescued_count)
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
        # external_link 外链 pin 没有正文（抓不到），也跳过 enrich。
        if p.source in ("github", "external_link"):
            continue
        cached = cache.get(pid)
        # 当前 schema 且不需要重试 → 复用；老 schema / 上次没读到 PDF → 重抽。
        if cached is not None and not cache.needs_reenrich(pid, bool(p.pdf_url)):
            enrich_cache_hits += 1
            _apply_enrichment(p, cached)
            continue
        if cached is not None:
            enrich_refresh += 1
        try:
            authors_text = "、".join(a.name for a in (p.authors or [])[:8])
            # 读 PDF 首页文本喂给抽取器：真实单位脚注 / 平台型号几乎只在首页，
            # 摘要里没有 → 不给 PDF 的话机构/平台只能靠猜（OASIS 把 G1 猜成 H1）。
            pdf_text = ""
            # openreview / conf 源量大，不逐篇下 PDF（只用 abstract 抽取），避免拖垮 CI。
            if p.pdf_url and (p.source or "") not in ("openreview", "conf"):
                try:
                    pdf_text, pc = fetch_head_text(p.pdf_url)
                    if pc > 0 and not p.page_count:
                        p.page_count = pc  # 顺带回填页数（longer_paper 评分用）
                except Exception as ex:
                    log.debug("head text extract failed for %s: %s", pid, ex)
            e = enrich_paper(p.title, p.abstract or p.tldr_zh or p.title, authors_text, pdf_text)
            enrich_calls += 1
            cache.put(pid, e, pdf_ok=bool(pdf_text), review_ok=getattr(e, "review_ok", True))
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
        src = p.source or ""
        # GitHub 开源仓不扫 demo 视频（卡片不展示视频角标，扫了只是膨胀缓存）。
        # 视频源卡（video_youtube / video_bilibili）本身就是一条视频，demo_videos
        # 已在 video_channels 里预填好；这里若再扫 abstract/项目页会返回 [] 把它
        # 覆盖清空，所以直接跳过。
        if src in ("github", "external_link", "openreview", "conf") or src.startswith("video_"):
            continue
        try:
            videos = enrich_paper_videos(p, cache)
            # 防御：万一扫不到也别清掉卡片自带的 demo_videos。
            p.demo_videos = videos or p.demo_videos
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


_OPENREVIEW_STATE_PATH = cfg.REPO_ROOT / "data" / "openreview_state.json"


def _openreview_should_fetch(refresh_days: int, venue_sig: str = "") -> bool:
    """会议数据是静态的，refresh_days 天才重抓一次。盘上还没 openreview 卡 → 必抓。
    另：venueid 集合变了（新增了会议季/会议）也立即重抓，不等冷却。"""
    if refresh_days <= 0:
        return True
    if not any(cfg.PAPERS_DIR.glob("openreview-*.json")):
        return True
    try:
        st = json.loads(_OPENREVIEW_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return True
    if venue_sig and st.get("venue_sig", "") != venue_sig:
        return True  # venueid 集合变化 → 立即抓新会议
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st.get("last_fetch", ""))).days
        return age >= refresh_days
    except Exception:
        return True


def _openreview_mark_fetched(venue_sig: str = "") -> None:
    try:
        _OPENREVIEW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _OPENREVIEW_STATE_PATH.write_text(
            json.dumps({"last_fetch": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "venue_sig": venue_sig}),
            encoding="utf-8")
    except Exception:
        pass


_CONF_STATE_PATH = cfg.REPO_ROOT / "data" / "conf_papers_state.json"


def _conf_should_fetch(refresh_days: int, sig: str = "") -> bool:
    if refresh_days <= 0:
        return True
    if not _CONF_STATE_PATH.exists():
        return True  # 首次：必抓
    try:
        st = json.loads(_CONF_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return True
    if sig and st.get("sig", "") != sig:
        return True  # venue 配置变了 → 立即重抓
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(st.get("last_fetch", ""))).days
        return age >= refresh_days
    except Exception:
        return True


def _conf_mark_fetched(sig: str = "") -> None:
    try:
        _CONF_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONF_STATE_PATH.write_text(
            json.dumps({"last_fetch": dt.datetime.now(dt.timezone.utc).isoformat(), "sig": sig}),
            encoding="utf-8")
    except Exception:
        pass


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

    # ----- 论文配套代码：从论文摘要 / 相关链接里抽 GitHub 链接，低 star 也收 -----
    # 很多论文在摘要/项目页贴自己的 github(.io)，即便 star 不高也是该方向的一手代码。
    # 这里按 owner/repo 直接拉元数据（不受 star 阈值限制），打上 paper_companion 标，
    # 同样过 judge_repo 把关（砍掉非算法/无关仓）。
    try:
        seen_full = {d["full_name"].lower() for d in repos}
        existing_slugs = {jp.stem for jp in cfg.PAPERS_DIR.glob("github-*.json")}
        link_budget = int(os.environ.get("REDPAPER_GH_PAPER_LINKS", "40") or "40")
        candidates: list[str] = []
        cand_seen: set[str] = set()
        for p in _existing_papers().values():
            if (p.source or "") in ("github", "external_link"):
                continue
            hay = (p.abstract or "")
            for rl in (p.related_links or []):
                hay += " " + (rl.get("url") or "")
            for full in gh_src.extract_repo_links(hay):
                fl = full.lower()
                if fl in seen_full or fl in cand_seen:
                    continue
                if gh_src._slug(full) in existing_slugs:
                    continue  # 已有这张卡，省一次 API
                cand_seen.add(fl)
                candidates.append(full)
        linked = 0
        for full in candidates:
            if linked >= link_budget:
                break
            d = gh_src.fetch_repo(full)
            if not d:
                continue
            d["paper_linked"] = True
            repos.append(d)
            linked += 1
        if linked:
            log.info("github: +%d paper-linked repos (low-star ok)", linked)
    except Exception as e:
        log.warning("github paper-linked collection failed: %s", e)

    cache = JudgeCache(cfg.REPO_ROOT / "data" / "judge_cache.json")
    judged = kept = dropped = cache_hits = 0
    failed_ids: set[str] = set()  # 本轮 judge 瞬时失败的 repo —— reconcile 时别误删它们的旧卡
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
                # 单个 repo 瞬时失败（网络/限流）：记下来，reconcile 时保留它的旧卡，
                # 别因为这一轮没判出来就把好仓误删。
                log.warning("repo judge failed for %s: %s; skipping (keep old card)", pid, e)
                failed_ids.add(pid)
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
    # cache 清空重判后才能正确生效）。只在成功路径执行。failed_ids（本轮瞬时失败
    # 没判出来的）也一并保留，避免单个 repo 的网络抖动误删它的好卡。
    _reconcile_github(set(out.keys()) | failed_ids)

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
            # ★ fresh 为主：paper(fresh) 携带本轮重新算过的 judge / enrich
            # (institutions/platform/...) / demo_videos / score，必须保留。只从
            # cached 继承「贵且无需重算」的翻译 + 封面 + 页数。
            # 历史坑：以前这里 `paper = cached`，把本轮 re-judge/re-enrich/视频
            # 全丢了 → 活跃论文（在 fresh 里）的标签永远停在首次入库值。
            is_gh = (paper.source or "") == "github"
            # 频道：github 用 fresh 判定的方向（不并 cached，免得把旧 open-source
            # 并回来）；其它源与 cached 取并集（保持历史行为）。
            if not is_gh:
                for c in cached.channels:
                    if c not in paper.channels:
                        paper.channels.append(c)
            # 日期：published 取最早（保留 first-publish），updated 取最新。
            if cached.published and (not paper.published or cached.published < paper.published):
                paper.published = cached.published
            if cached.updated and cached.updated > (paper.updated or ""):
                paper.updated = cached.updated
            # 翻译：fresh 一般没翻（cn_news / github 自带的除外），从 cached 继承。
            paper.title_zh = paper.title_zh or cached.title_zh
            paper.abstract_zh = paper.abstract_zh or cached.abstract_zh
            paper.tldr_zh = paper.tldr_zh or cached.tldr_zh
            paper.cover_zh = paper.cover_zh or cached.cover_zh
            # 封面 / 预览 / 页数：已渲染过的从 cached 继承，不重渲。
            paper.cover_image = paper.cover_image or cached.cover_image
            if not paper.preview_pages:
                paper.preview_pages = list(cached.preview_pages or [])
            if not paper.page_count:
                paper.page_count = cached.page_count
            if not paper.related_links:
                paper.related_links = list(cached.related_links or [])
            # source_tags 取并集（保留 manual_pin 等历史标记）。
            for tg in (cached.source_tags or []):
                if tg not in (paper.source_tags or []):
                    paper.source_tags.append(tg)
            # venue：本轮解析/抓到的优先，没有则继承 cached。venue_announced（"最新收录"
            # 重新冒泡）只由 _venue_backfill 在「存量 arXiv 论文刚被收录」时打，不在这里
            # 自动产生——否则 OpenReview/conf 批量导入的去年论文会被误当"今天新收录"刷屏。
            paper.venue = paper.venue or cached.venue
            paper.venue_announced = paper.venue_announced or cached.venue_announced

        # openreview / conf 源：一次可能几百篇，逐篇下载+渲染 PDF 会拖垮 CI → 用占位
        # 封面，不渲染（仍保留 pdf_url 供「下载 PDF」）。
        if not paper.cover_image and paper.pdf_url and (paper.source or "") not in ("openreview", "conf"):
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
    curated_ids: set = field(default_factory=set)  # 站长甄选高质量论文 id
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

        # 💎 站长甄选（config/curated.yaml）—— 高质量金标准。打徽章 + 加 source_tag
        # （让 scoring 的 curated 规则加分）。放在 score_paper 之前。
        if paper.id in self.curated_ids:
            if "curated" not in paper.source_tags:
                paper.source_tags.append("curated")
            paper.badges.append({"kind": "gem", "label": "💎 站长甄选"})

        # 🎓 会议/期刊徽章（venue）。刚检测到（venue_announced 在最近 14 天内）的
        # 用「🎉 最新收录」更醒目——对应"被 RSS/CoRL 收录后重新上 feed"。
        if paper.venue:
            recent = False
            if paper.venue_announced:
                try:
                    ann = dt.datetime.fromisoformat(paper.venue_announced[:10]).date()
                    recent = (dt.date.today() - ann).days <= 14
                except ValueError:
                    recent = False
            label = (f"🎉 最新收录 · {paper.venue}" if recent else f"🎓 {paper.venue}")
            paper.badges.append({"kind": "venue", "label": label})

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

    # conferences.json — 会议投稿倒计时数据（前端首页组件用，纯静态透传）。
    try:
        with (cfg.DATA_DIR / "conferences.json").open("w", encoding="utf-8") as f:
            json.dump({"conferences": cfg.load_conferences()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning("conferences.json write failed: %s", e)

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

    # curated.json —— 站长甄选「金标准」数据集（committed via site/data），供后续
    # 关键词挖掘 / judge few-shot / 相似度推荐 / 量化评测复用。
    curated = [p for p in all_papers if "curated" in (p.source_tags or [])]
    curated.sort(key=lambda p: (p.published or "", p.id), reverse=True)
    with (cfg.DATA_DIR / "curated.json").open("w", encoding="utf-8") as f:
        json.dump({
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "count": len(curated),
            "papers": [{
                "id": p.id,
                "title": p.title,
                "title_zh": p.title_zh or p.title,
                "channels": p.channels or [],
                "institutions": p.institutions or [],
                "method_tags": p.method_tags or [],
                "platform": p.platform or [],
                "published": p.published,
                "source": p.source,
                "abs_url": p.abs_url,
            } for p in curated],
        }, f, ensure_ascii=False, indent=2)

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
        # 全量作者名，仅供前端搜索用（显示仍取前 3）。通讯/导师常是最后一位，
        # 只存前 3 会导致搜不到（如搜导师名 Yue Wang 搜不到）。
        "authors_all": [a.name for a in p.authors],
        "primary_category": p.primary_category,
        "published": p.published,
        "channels": p.channels,
        "badges": p.badges,
        "cover_image": p.cover_image,
        "preview_pages": p.preview_pages,
        "arxiv_id": p.arxiv_id,
        "abs_url": p.abs_url,
        "pdf_url": p.pdf_url,
        "venue": p.venue or "",
        "venue_announced": p.venue_announced or "",
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
        # 来源标记（manual_pin / curated / paper_companion …），前端按需展示小标
        "source_tags": p.source_tags or [],
    }


def _build_enrichment_context(sources: cfg.SourcesConfig, fresh: dict[str, Paper]) -> EnrichmentContext:
    """Pull Phase 2 metadata only if those sources are enabled."""
    ctx = EnrichmentContext()
    try:
        ctx.curated_ids = cfg.load_curated_ids()
    except Exception as e:
        log.warning("load curated failed: %s", e)

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
    # 核心 6 类按关键词重算 channels（与历史一致，零回归）；自定义分类（channels.d）
    # 的成员关系由 assign_custom_channels（关键词 + 独立 judge）单独维护，这里只
    # 「保留」已判定的自定义成员（频道还在 config 里才保留），不靠关键词增删。
    core_channels = [c for c in channels if not c.is_custom]
    custom_ids = {c.id for c in channels if c.is_custom}
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

        # 视频卡（厂商 demo，source=video_youtube / video_bilibili）：豁免 prune
        # —— 标题常不含频道关键词，按普通论文规则会被误删。尽力按关键词归类，
        # 没命中就保留已有 channels（可能为空，只在「全部」露出），但绝不删除。
        if (paper.source or "").lower().startswith("video_"):
            matched = [c.id for c in channels
                       if _matches_channel(paper.title, paper.abstract, c)]
            new_ch = matched or paper.channels
            if new_ch != paper.channels:
                paper.channels = new_ch
                save_paper(paper, cfg.PAPERS_DIR)
            kept += 1
            continue

        pinned = (paper.source or "").lower() in ("manual_arxiv", "manual_xhs") \
                 or "manual_pin" in (paper.source_tags or [])

        matches = [c.id for c in core_channels if _matches_channel(paper.title, paper.abstract, c)]
        # 已被判进、且频道仍存在的自定义分类成员关系予以保留（不靠关键词重算）。
        preserved_custom = [cid for cid in (paper.channels or []) if cid in custom_ids]
        effective = matches + [cid for cid in preserved_custom if cid not in matches]

        if effective:
            if set(paper.channels) != set(effective):
                paper.channels = effective
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


_CUSTOM_STATE_PATH = cfg.REPO_ROOT / "data" / "custom_channels_state.json"


def _channel_backfill_sig(c: cfg.Channel) -> str:
    """决定「要不要重新往前收一个月」的指纹：取决于召回相关字段（关键词 / arxiv
    分类 / 回填天数）。注意不能用 channel_prompt_signature（那只含 desc+judge_prompt）——
    否则站长只改关键词时，sig 不变 → 永远不再回填，新关键词补不回历史。"""
    import hashlib
    basis = "\x00".join([
        "|".join(sorted(c.keywords or [])),
        "|".join(sorted(c.arxiv_categories or [])),
        str(c.backfill_days),
    ])
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _load_custom_state() -> dict:
    if _CUSTOM_STATE_PATH.exists():
        try:
            return json.loads(_CUSTOM_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_custom_state(state: dict) -> None:
    _CUSTOM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _CUSTOM_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_CUSTOM_STATE_PATH)


def _doi_from_url(url: str) -> str:
    """从论文 URL 抠出 DOI。science.org / 通用链接里直接含 10.xxxx/...；nature.com
    的 /articles/<id> 没写 DOI，按 10.1038/<id> 拼。"""
    m = re.search(r"10\.\d{4,9}/[^\s?#\"'<>]+", url or "")
    if m:
        return m.group(0).rstrip(").,;")
    m = re.search(r"nature\.com/articles/([^\s?#/]+)", url or "")
    if m:
        return f"10.1038/{m.group(1)}"
    return ""


def _crossref_meta(url: str) -> dict:
    """用 Crossref（免费、无需 key）按 DOI 取作者 / 会议期刊 / 日期 / 摘要。
    取不到返回 {}。"""
    doi = _doi_from_url(url)
    if not doi:
        return {}
    try:
        import requests
        r = requests.get(
            f"https://api.crossref.org/works/{doi}",
            params={"mailto": "redpaper@users.noreply.github.com"},
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        msg = r.json().get("message") or {}
    except Exception as e:
        log.debug("crossref failed for %s: %s", doi, e)
        return {}
    authors = []
    for a in (msg.get("author") or []):
        nm = " ".join(x for x in [a.get("given"), a.get("family")] if x).strip() or a.get("name") or ""
        if nm:
            authors.append(nm)
    ct = msg.get("container-title") or []
    venue = ct[0] if ct else ""
    # 日期：issued > published-print > published-online
    date = ""
    for key in ("issued", "published-print", "published-online", "created"):
        parts = ((msg.get(key) or {}).get("date-parts") or [[]])[0]
        if parts:
            date = "-".join(f"{int(x):02d}" if i else f"{int(x):04d}" for i, x in enumerate(parts[:3]))
            break
    abstract = msg.get("abstract") or ""
    abstract = re.sub(r"<[^>]+>", "", abstract).strip()  # 去 JATS 标签
    return {
        "authors": authors,
        "venue": venue,
        "published": date,
        "abstract": abstract,
        "title": (msg.get("title") or [""])[0],
    }


def _load_custom_examples(custom_channels: list[cfg.Channel]) -> list[Paper]:
    """把每个自定义分类里站长填的「示例高质量论文」做成 pin 卡片（立即上墙、
    跳过 judge），同时归到对应频道。

    - arXiv 链接：走 arxiv API 抓全量元数据（封面/摘要/作者）。
    - 非 arXiv 链接（Nature / Science 等，本站抓不到正文）：只要给了 title，就做成
      一张「外链 pin 卡」（source=external_link，无封面/摘要，点击直达原文）。没给
      title 的非 arXiv 链接没法做卡，跳过并告警。
    """
    import hashlib
    arxiv_entries: list[dict] = []
    out: list[Paper] = []
    for c in custom_channels:
        for ex in (c.examples or []):
            if not isinstance(ex, dict):
                continue
            ref = (ex.get("url") or ex.get("id") or "").strip()
            if not ref:
                continue
            title = (ex.get("title") or "").strip()
            aid = manual_arxiv_source._extract_id(ref)
            if aid:
                arxiv_entries.append({"id": aid, "note": title, "channels": [c.id]})
            elif ref.startswith("http") and title:
                pid = "ext-" + hashlib.sha1(ref.encode("utf-8")).hexdigest()[:10]
                # Crossref 补作者 / 会议 / 摘要 / 日期（拿不到就只有 title）。
                meta = _crossref_meta(ref)
                out.append(Paper(
                    id=pid,
                    source="external_link",
                    title=title or meta.get("title", ""),
                    abstract=meta.get("abstract", ""),
                    authors=[Author(name=n) for n in meta.get("authors", [])],
                    abs_url=ref,
                    pdf_url="",
                    published=(ex.get("date") or meta.get("published") or ""),
                    venue=meta.get("venue", ""),
                    channels=[c.id],
                    source_tags=["manual_pin"],
                ))
            else:
                log.warning("custom example skipped (需要 arXiv 链接，或给非 arXiv 链接配 title): %s", ref)
    if arxiv_entries:
        try:
            out.extend(manual_arxiv_source._fetch_entries(arxiv_entries, []))
        except Exception as e:
            log.warning("custom arxiv examples fetch failed: %s", e)
    return out


def assign_custom_channels(custom_channels: list[cfg.Channel],
                           fresh_ids: set[str], budget: int) -> None:
    """对盘上所有论文重算自定义分类成员关系（回填 + 自愈）：

      - 不命中频道关键词 → 移除该频道（纯本地、不花钱）。
      - 命中关键词 → 查 CustomChannelCache；缺失则在 budget 内调一次独立 judge；
        budget 用尽就这轮先不动（下轮继续，多 build 摊销）。
      - judge 收下 → 加入该频道；判否 → 移除。

    fresh 这轮已在 _judge_filter 捞回逻辑里判过的，缓存命中不再付费。
    无 API key（dryrun）时退化为纯关键词命中即归类。
    """
    if not custom_channels or not cfg.PAPERS_DIR.exists():
        return
    ccache = CustomChannelCache(cfg.REPO_ROOT / "data" / "custom_judge_cache.json")
    calls = 0
    changed = 0
    sigs = {c.id: channel_prompt_signature(c) for c in custom_channels}
    for jp in cfg.PAPERS_DIR.glob("*.json"):
        try:
            paper = load_paper(jp)
        except Exception:
            continue
        if (paper.source or "") == "github":
            continue
        # 钉过的论文（manual_pin / 示例 / manual_arxiv）频道由站长指定，自动归类
        # 不要去动它（否则示例 pin 可能被独立 judge 判否、丢掉所属频道）。
        if "manual_pin" in (paper.source_tags or []) or (paper.source or "") in ("manual_arxiv", "external_link"):
            continue
        dirty = False
        for c in custom_channels:
            matched = _matches_channel(paper.title, paper.abstract, c)
            if not matched:
                if c.id in (paper.channels or []):
                    paper.channels.remove(c.id)
                    dirty = True
                continue
            sig = sigs[c.id]
            verdict = ccache.get(paper.id, c.id, sig)
            if verdict is None:
                if calls >= budget:
                    continue  # 这轮预算用尽，成员关系暂不动，下轮再判
                try:
                    jj = judge_paper_for_channel(
                        paper.title, paper.abstract or paper.tldr_zh or paper.title, c)
                    ccache.put(paper.id, c.id, jj, sig)
                    calls += 1
                    verdict = jj.relevant
                except JudgeUnavailable:
                    verdict = True  # 无 token → 关键词命中即收
                except Exception as e:
                    log.warning("custom judge failed for %s @ %s: %s", paper.id, c.id, e)
                    verdict = None
            if verdict is True:
                if c.id not in (paper.channels or []):
                    paper.channels.append(c.id)
                    dirty = True
            elif verdict is False:
                if c.id in (paper.channels or []):
                    paper.channels.remove(c.id)
                    dirty = True
        if dirty:
            save_paper(paper, cfg.PAPERS_DIR)
            changed += 1
    ccache.save()
    log.info("custom channels: %d judge calls, %d papers re-tagged", calls, changed)


_VENUE_STATE_PATH = cfg.REPO_ROOT / "data" / "venue_check_state.json"


def _venue_backfill(budget: int = 50) -> None:
    """滚动复查存量 arXiv 论文的 venue：很多论文先挂 arXiv，几个月后才被 ICRA/RSS/
    CoRL 等收录，作者会更新 arXiv `comment`。我们的常规抓取只覆盖最近窗口，老论文
    的"被收录"信号收不到。这里每轮抽一批（budget 篇、近 21 天没查过的）重查 arXiv
    元数据，解析到 venue 就回填 + 记 venue_announced=今天（→ 重新冒泡到 feed）。"""
    if budget <= 0 or not cfg.PAPERS_DIR.exists():
        return
    import arxiv
    # 状态：arxiv_id -> 上次检查日期，避免每轮重查同一批
    state: dict[str, str] = {}
    if _VENUE_STATE_PATH.exists():
        try:
            state = json.loads(_VENUE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=21)).isoformat()

    # 候选：有 arxiv_id、还没 venue、近 21 天没查过的；按发布日倒序（近的更可能刚被收录）
    cand: list[Paper] = []
    by_aid: dict[str, list[Paper]] = {}
    for jp in cfg.PAPERS_DIR.glob("*.json"):
        try:
            p = load_paper(jp)
        except Exception:
            continue
        if not p.arxiv_id or p.venue:
            continue
        by_aid.setdefault(p.arxiv_id, []).append(p)
        if state.get(p.arxiv_id, "") >= cutoff:
            continue
        cand.append(p)
    if not cand:
        return
    cand.sort(key=lambda p: (p.published or "", p.id), reverse=True)
    cand = cand[:budget]
    ids = sorted({p.arxiv_id for p in cand})

    found = 0
    try:
        client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=3)
        for r in client.results(arxiv.Search(id_list=ids)):
            aid = re.sub(r"v\d+$", "", r.get_short_id())
            v = parse_venue(getattr(r, "comment", "") or "")
            # 标记已查（无论有没有 venue）
            state[aid] = today
            if not v:
                continue
            for p in by_aid.get(aid, []):
                if p.venue:
                    continue
                p.venue = v
                p.venue_announced = today
                save_paper(p, cfg.PAPERS_DIR)
                found += 1
    except Exception as e:
        log.warning("venue backfill failed: %s", e)

    # 没在结果里出现的也记一次 today，免得每轮都重查（arXiv 偶尔漏返回）
    for aid in ids:
        state.setdefault(aid, today)
    try:
        _VENUE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _VENUE_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    if found:
        log.info("venue backfill: %d papers newly tagged with venue", found)


def _expand_custom_keywords(custom_channels: list[cfg.Channel]) -> None:
    """构建时用 LLM 给每个自定义分类补充召回关键词（缓存，改了定义/标准/关键词才重扩）。
    只拓宽召回，精度仍由该分类独立 judge 把关。无 API key 时静默跳过。"""
    if not custom_channels:
        return
    cache = KeywordCache(cfg.REPO_ROOT / "data" / "custom_keyword_cache.json")
    changed = False
    for c in custom_channels:
        sig = keyword_expand_signature(c)
        extra = cache.get(c.id, sig)
        if extra is None:
            try:
                extra = expand_channel_keywords(c)
                cache.put(c.id, sig, extra)
                changed = True
                log.info("keyword expand[%s]: +%d AI keywords", c.id, len(extra))
            except JudgeUnavailable:
                extra = []
            except Exception as e:
                log.warning("keyword expand[%s] failed: %s", c.id, e)
                extra = []
        have = {k.lower() for k in (c.keywords or [])}
        for k in extra:
            if k and k.lower() not in have:
                c.keywords.append(k)
                have.add(k.lower())
    if changed:
        cache.save()


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg.ensure_dirs()
    channels = cfg.load_channels()
    sources = cfg.load_sources()
    custom_channels = [c for c in channels if c.is_custom]
    if custom_channels:
        log.info("custom channels active: %s", ", ".join(c.id for c in custom_channels))
        # AI 扩展自定义分类的召回关键词（在 retag / fetch 之前，让后续都用上更全的词）。
        _expand_custom_keywords(custom_channels)

    # 1) Realign existing cached papers with the current channels.yaml BEFORE
    #    fetching anything. Off-topic papers are pruned so the feed stays
    #    aligned with what the owner currently cares about.
    retag_and_prune(channels)

    fresh: dict[str, Paper] = {}
    if sources.arxiv_enabled:
        fresh.update(arxiv_source.fetch_all(channels, sources))

        # 自定义分类「首月回填」：新加 / prompt 改过的频道，一次性用更宽 lookback
        # （backfill_days，默认随表单 30 天）把最近一个月命中其关键词的 arxiv 论文
        # 也抓进来，交给 _judge_filter 的捞回逻辑判定。靠 sig + state 文件保证只做
        # 一次，避免每天都重抓一个月。
        if custom_channels:
            from dataclasses import replace
            state = _load_custom_state()
            for c in custom_channels:
                if c.backfill_days <= 0:
                    continue
                sig = _channel_backfill_sig(c)
                st = state.get(c.id) or {}
                if st.get("sig") == sig and st.get("backfilled"):
                    continue
                wider = replace(
                    sources,
                    arxiv_lookback_days=max(c.backfill_days, sources.arxiv_lookback_days),
                )
                try:
                    got = arxiv_source.fetch_all([c], wider)
                    for pid, paper in got.items():
                        if pid not in fresh:
                            fresh[pid] = paper
                    state[c.id] = {"sig": sig, "backfilled": True}
                    log.info("custom backfill[%s]: +%d papers over %d days",
                             c.id, len(got), wider.arxiv_lookback_days)
                except Exception as e:
                    log.warning("custom backfill[%s] failed: %s", c.id, e)
            _save_custom_state(state)

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

    # 自定义分类的「示例高质量论文」→ 当 pin 卡片立即上墙（跳过 judge），归到对应频道。
    if custom_channels:
        try:
            for p in _load_custom_examples(custom_channels):
                fresh[p.id] = p
        except Exception as e:
            log.warning("custom examples load failed: %s", e)

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

    # ----- 会议官网源（OpenReview：CoRL/ICLR/NeurIPS 接收论文）----------
    # 按 venueid 取接收论文 → 频道关键词过滤出机器人相关 → 落 source=openreview。
    # 与 arXiv 靠标题去重；不渲染封面（占位）。refresh_days 节流（会议数据静态）。
    # venue_ids = sources.yaml 显式配置 + 从 conferences.yaml 的 openreview 模板按
    # 当年/次年自动推导（这样新会议季放榜后无需手动加 venueid，自动尝试、空就跳过）。
    or_venue_ids = list(getattr(sources, "openreview_venue_ids", None) or [])
    try:
        _yr = dt.date.today().year
        for conf in cfg.load_conferences():
            tmpl = conf.get("openreview")
            if not tmpl:
                continue
            for y in (_yr - 1, _yr, _yr + 1):
                vid = tmpl.replace("{year}", str(y))
                if vid not in or_venue_ids:
                    or_venue_ids.append(vid)
    except Exception as e:
        log.warning("openreview venueid auto-derive failed: %s", e)

    _or_sig = "|".join(sorted(or_venue_ids))
    if getattr(sources, "openreview_enabled", False) and or_venue_ids:
        if _openreview_should_fetch(getattr(sources, "openreview_refresh_days", 14), _or_sig):
            try:
                from .sources import openreview as _openreview
                or_papers = _openreview.fetch_papers(
                    or_venue_ids, channels,
                    max_per_venue=getattr(sources, "openreview_max_per_venue", 80),
                )
                added = 0
                for p in or_papers:
                    if p.id not in fresh:
                        fresh[p.id] = p
                        added += 1
                log.info("openreview: +%d papers", added)
                if or_papers:
                    _openreview_mark_fetched(_or_sig)
            except Exception as e:
                log.warning("openreview step failed: %s", e)
        else:
            log.info("openreview: within refresh window, skipping fetch")

    # ----- 会议/期刊源（Semantic Scholar 按 venue 检索）------------------
    # 补 OpenReview 覆盖不到的 CVPR/ICCV/ECCV/RA-L/ICML 等的「去年中稿」论文。
    if getattr(sources, "conf_papers_enabled", False) and getattr(sources, "conf_papers_venues", None):
        import hashlib as _hl
        conf_sig = _hl.sha1(json.dumps(sources.conf_papers_venues, sort_keys=True).encode()).hexdigest()[:12]
        if _conf_should_fetch(getattr(sources, "conf_papers_refresh_days", 14), conf_sig):
            try:
                from .sources import conf_papers as _conf
                cf_papers = _conf.fetch_papers(
                    sources.conf_papers_venues, channels,
                    max_per_venue=getattr(sources, "conf_papers_max_per_venue", 40),
                )
                added = tagged = 0
                for p in cf_papers:
                    if p.id in fresh:
                        if not fresh[p.id].venue:
                            fresh[p.id].venue = p.venue
                        tagged += 1
                    else:
                        fresh[p.id] = p
                        added += 1
                log.info("conf_papers: +%d new, %d already-present tagged", added, tagged)
                if cf_papers:
                    _conf_mark_fetched(conf_sig)
            except Exception as e:
                log.warning("conf_papers step failed: %s", e)
        else:
            log.info("conf_papers: within refresh window, skipping fetch")

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
    fresh = _judge_filter(fresh, custom_channels)
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

    # 滚动复查存量 arXiv 论文的「被会议/期刊收录」信号（更新 venue + 重新冒泡）。
    try:
        _venue_backfill(int(os.environ.get("REDPAPER_VENUE_BACKFILL", "50") or "50"))
    except Exception as e:
        log.warning("venue backfill step failed: %s", e)

    # Re-enrich existing papers too (so badges/news stay fresh even if the paper
    # was fetched on an earlier day). Also back-fill translation fields the
    # current model expects (e.g. cover_zh was added later).
    #
    # 滚动 enrich backfill：存量里 enrich schema 过期（老的瞎猜版 / 没读过 PDF）的
    # 论文，每轮补抽一批（读 PDF + reviewer，限量防 CI 超时）。按发布日期倒序优先
    # 修近期高曝光论文。窗口外的旧论文（如 OASIS 6/07）就是靠这条慢慢纠正过来。
    enrich_cache = EnrichCache(cfg.REPO_ROOT / "data" / "enrich_cache.json")
    # 默认每轮只补 30 篇（读 PDF + writer + reviewer 较重，避免日常 build 撞 50min
    # 超时 / 成本突增）；一次性全站迁移用 workflow_dispatch 的 enrich_backfill 调大。
    backfill_budget = int(os.environ.get("REDPAPER_ENRICH_BACKFILL", "30") or "30")
    backfilled = 0
    all_papers = list(_existing_papers().values())
    all_papers.sort(key=lambda p: (p.published or "", p.id), reverse=True)
    for paper in all_papers:
        if paper.id in fresh:
            continue  # already enriched in process_new_papers

        if (paper.source or "") != "github" and backfilled < backfill_budget \
                and enrich_cache.needs_reenrich(paper.id, bool(paper.pdf_url)):
            try:
                authors_text = "、".join(a.name for a in (paper.authors or [])[:8])
                pdf_text = ""
                if paper.pdf_url:
                    pdf_text, pc = fetch_head_text(paper.pdf_url)
                    if pc > 0 and not paper.page_count:
                        paper.page_count = pc
                e = enrich_paper(paper.title, paper.abstract or paper.tldr_zh or paper.title,
                                 authors_text, pdf_text)
                enrich_cache.put(paper.id, e, pdf_ok=bool(pdf_text), review_ok=getattr(e, "review_ok", True))
                _apply_enrichment(paper, e)
                backfilled += 1
            except EnrichUnavailable:
                pass
            except Exception as ex:
                log.warning("enrich backfill failed for %s: %s", paper.id, ex)

        if not _is_translated(paper):
            t = translate_with_retry(paper.title, paper.abstract)
            paper.title_zh = t.title_zh or paper.title_zh or paper.title
            paper.abstract_zh = t.abstract_zh or paper.abstract_zh or paper.abstract
            paper.tldr_zh = t.tldr_zh or paper.tldr_zh
            paper.cover_zh = t.cover_zh or paper.cover_zh or paper.tldr_zh
            log.info("back-fill translation: %s", paper.id)

        ctx.apply(paper)
        save_paper(paper, cfg.PAPERS_DIR)

    if backfilled:
        enrich_cache.save()
        log.info("enrich backfill: re-enriched %d schema-outdated existing papers", backfilled)

    # 自定义分类成员关系重算 / 回填（关键词 + 独立 judge，预算限量、多 build 摊销）。
    if custom_channels:
        custom_budget = int(os.environ.get("REDPAPER_CUSTOM_BACKFILL", "60") or "60")
        try:
            assign_custom_channels(custom_channels, set(fresh.keys()), custom_budget)
        except Exception as e:
            log.warning("assign_custom_channels failed: %s", e)

    all_papers = list(_existing_papers().values())
    write_feed(all_papers)
    sorted_papers = sorted(all_papers, key=lambda p: (p.published, p.id), reverse=True)
    write_markdown_digest(sorted_papers)
    write_rss(sorted_papers)
    log.info("feed written: %d papers total", len(all_papers))

    # ----- P6: monthly digest（当前月）-------------------------------------
    # 只重生成本月份，避免每天把所有月份都烧一遍 LLM。生成后写完 monthly_index。
    _refresh_current_month_digest(sorted_papers)

    # ----- 数据可视化预聚合 → site/data/stats.json ------------------------
    try:
        from .stats import write_stats
        write_stats(all_papers, channels)
    except Exception as e:
        log.warning("stats step failed: %s", e)


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
