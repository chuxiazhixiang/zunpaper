"""One-off: ingest 2026 arXiv papers from awesome_papers.md into redpaper.

For every paper found in the 5 target sections of `awesome_papers.md`
(Loco-Manipulation and Whole-Body-Control / Manipulation / Teleoperation /
Locomotion / Sim-to-Real) that was published in 2026, we:

  1. Parse the markdown, extract (channel_id, arxiv_id, optional website).
  2. Skip ones already on the site (papers/{slug}.json exists).
  3. Batch-fetch the rest via the arXiv API.
  4. Construct Paper objects, channel-tag them per the section they came
     from, and run them through the *exact same* pipeline as a daily build:
        - _judge_filter  (DeepSeek quality gate)
        - _enrich_papers (institutions + method tags)
        - process_new_papers (cover + translation + badges)
        - write_feed / digest / rss
  5. Dump a tmp/awesome-import-report.md so the user can see who got in,
     who got cut, and why.

Run:
    DEEPSEEK_API_KEY=sk-... python scripts/import_awesome.py
    DEEPSEEK_API_KEY=sk-... python scripts/import_awesome.py --year 2025  # also pull 2025
    DEEPSEEK_API_KEY=sk-... python scripts/import_awesome.py --limit 5    # smoke test
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import arxiv  # noqa: E402

from redpaper import config as cfg  # noqa: E402
from redpaper import build  # noqa: E402
from redpaper.models import Author, Paper, save_paper, load_paper  # noqa: E402
from redpaper.judge import JudgeCache, judge_paper, JudgeUnavailable  # noqa: E402
from redpaper.enrich import EnrichCache, enrich_paper, EnrichUnavailable  # noqa: E402
from redpaper.digest import write_markdown_digest, write_rss  # noqa: E402


# ---- Section → channel ID -----------------------------------------------

SECTION_TO_CHANNEL = {
    "Loco-Manipulation and Whole-Body-Control": "loco-manip-wbc",
    "Manipulation": "manipulation",
    "Teleoperation": "teleop",
    "Locomotion": "locomotion",
    "Sim-to-Real": "sim2real",
}


_ARXIV_LINK_RE = re.compile(r"https?://arxiv\.org/abs/([\w.\-]+)")
_WEBSITE_RE = re.compile(r"\[website\]\(([^)]+)\)")
_ARXIV_DATE_RE = re.compile(r"\[arXiv (\d{4})\.(\d{1,2})\]")


def parse_awesome(md_path: Path, *, year: int = 2026) -> list[dict]:
    """Return list of `{channel, arxiv_id, title_hint, website}` for the
    target year × target sections."""
    text = md_path.read_text(encoding="utf-8")
    rows: list[dict] = []
    current_section: str | None = None
    for line in text.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            current_section = m.group(1).strip()
            continue
        if current_section not in SECTION_TO_CHANNEL:
            continue
        am = _ARXIV_LINK_RE.search(line)
        if not am:
            continue
        date_m = _ARXIV_DATE_RE.search(line)
        if not date_m:
            continue
        try:
            yr = int(date_m.group(1))
        except ValueError:
            continue
        if yr != year:
            continue

        arxiv_id = re.sub(r"v\d+$", "", am.group(1))
        # 第一个 `, ` 之后到下一个 `,` / EOL 之间是标题
        rest = line[line.find(", ") + 2 :] if ", " in line else line
        title_hint = rest.split(", [website]")[0].split(", [arXiv")[0].split(",")[0]
        site_m = _WEBSITE_RE.search(line)
        rows.append({
            "channel": SECTION_TO_CHANNEL[current_section],
            "arxiv_id": arxiv_id,
            "title_hint": title_hint.strip(" ,*🌟"),
            "website": site_m.group(1) if site_m else "",
        })
    return rows


# ---- Batch fetch via arxiv API ------------------------------------------

def _existing_arxiv_ids() -> set[str]:
    """Already-on-disk arXiv IDs (so we don't re-pay translate/cover/judge)."""
    out: set[str] = set()
    for jp in cfg.PAPERS_DIR.glob("*.json"):
        try:
            p = load_paper(jp)
            if p.arxiv_id:
                out.add(p.arxiv_id)
        except Exception:
            pass
    return out


def fetch_arxiv_batch(arxiv_ids: list[str], *, chunk_size: int = 5,
                      delay_seconds: float = 8.0,
                      between_chunks: float = 8.0) -> dict[str, "arxiv.Result"]:
    """Batch-fetch metadata, return {arxiv_id_base: Result}.

    arxiv API 对 burst 非常敏感，68 IDs 一次性塞会 429，连续 20-IDs/chunk 也
    会触发。实战发现 chunk_size=5 + delay 8s 之间最稳。失败的 chunk 会被记
    录，全部跑完再统一从命令行重跑（已存在的 paper 跳过去重）。
    """
    out: dict[str, "arxiv.Result"] = {}
    if not arxiv_ids:
        return out
    client = arxiv.Client(page_size=chunk_size, delay_seconds=delay_seconds,
                          num_retries=4)
    n = len(arxiv_ids)
    failed_chunks: list[list[str]] = []
    for start in range(0, n, chunk_size):
        chunk = arxiv_ids[start : start + chunk_size]
        logging.info("arxiv: fetching ids %d–%d / %d (chunk size %d)",
                     start + 1, min(start + chunk_size, n), n, len(chunk))
        search = arxiv.Search(id_list=chunk)
        got_before = len(out)
        try:
            for r in client.results(search):
                full = r.get_short_id()
                base = re.sub(r"v\d+$", "", full)
                out[base] = r
        except Exception as e:
            logging.warning("arxiv chunk %d–%d error: %s (got %d so far)",
                            start + 1, start + chunk_size, e, len(out))
        # 如果整 chunk 都没拿到 → 标记失败，最后单条逐个重试
        if len(out) == got_before:
            failed_chunks.append(chunk)
        time.sleep(between_chunks)

    # ----- 失败 chunk 单条重试，每条 8s 间隔 ----------------------------
    if failed_chunks:
        failed_ids = [i for c in failed_chunks for i in c]
        logging.info("retrying %d failed IDs one by one ...", len(failed_ids))
        for i, aid in enumerate(failed_ids, 1):
            if aid in out:
                continue
            try:
                for r in client.results(arxiv.Search(id_list=[aid])):
                    full = r.get_short_id()
                    base = re.sub(r"v\d+$", "", full)
                    out[base] = r
                    logging.info("  [%d/%d] retry %s OK", i, len(failed_ids), aid)
            except Exception as e:
                logging.warning("  [%d/%d] retry %s failed: %s",
                                i, len(failed_ids), aid, e)
            time.sleep(8.0)
    return out


def make_paper(row: dict, r: "arxiv.Result") -> Paper:
    arxiv_id_full = r.get_short_id()
    arxiv_id_base = re.sub(r"v\d+$", "", arxiv_id_full)
    slug = "arxiv-" + arxiv_id_base.replace(".", "-").replace("/", "-").lower()
    title = (r.title or "").strip().replace("\n", " ")
    abstract = (r.summary or "").strip().replace("\n", " ")

    paper = Paper(
        id=slug,
        source="arxiv",
        title=title,
        abstract=abstract,
        authors=[Author(name=a.name) for a in r.authors],
        primary_category=r.primary_category or "",
        categories=list(r.categories or []),
        published=r.published.date().isoformat() if r.published else "",
        updated=r.updated.date().isoformat() if r.updated else "",
        arxiv_id=arxiv_id_base,
        pdf_url=r.pdf_url or "",
        abs_url=r.entry_id or "",
        channels=[row["channel"]],
        source_tags=["awesome_import"],
    )
    if row.get("website"):
        paper.related_links.append({
            "source": "project",
            "source_name": "项目主页",
            "title": "Project page",
            "url": row["website"],
        })
    return paper


# ---- Drive ---------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--awesome", default="awesome_papers.md",
                    help="path to awesome_papers.md (relative to repo root)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap on # of papers to import (0 = no cap)")
    ap.add_argument("--pace", type=float, default=1.2,
                    help="sec between judge / enrich LLM calls")
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan, don't fetch / judge / save")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("import_awesome")

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("ERROR: DEEPSEEK_API_KEY not set", file=sys.stderr)
        return 1

    md = ROOT / args.awesome
    rows = parse_awesome(md, year=args.year)
    log.info("parsed %d candidates from %s (year=%s)", len(rows), md, args.year)
    if args.limit:
        rows = rows[: args.limit]

    existing = _existing_arxiv_ids()
    fresh_rows = [r for r in rows if r["arxiv_id"] not in existing]
    log.info("after dedupe: %d to fetch (%d already on site)",
             len(fresh_rows), len(rows) - len(fresh_rows))

    if args.dry_run:
        print(f"would fetch {len(fresh_rows)} papers across 5 channels:")
        from collections import Counter
        for ch, n in Counter(r["channel"] for r in fresh_rows).items():
            print(f"  {ch}: {n}")
        return 0

    cfg.ensure_dirs()

    # ---- 1. Fetch metadata from arXiv -----------------------------------
    arxiv_ids = [r["arxiv_id"] for r in fresh_rows]
    log.info("fetching arXiv metadata for %d ids ...", len(arxiv_ids))
    metas = fetch_arxiv_batch(arxiv_ids)
    log.info("arxiv returned %d / %d", len(metas), len(arxiv_ids))

    fresh: dict[str, Paper] = {}
    not_found: list[dict] = []
    for row in fresh_rows:
        r = metas.get(row["arxiv_id"])
        if not r:
            not_found.append(row)
            continue
        p = make_paper(row, r)
        if p.id in fresh:
            # 同篇 paper 在 awesome 文件里被列在多个 section 里：合并 channel
            for c in p.channels:
                if c not in fresh[p.id].channels:
                    fresh[p.id].channels.append(c)
            continue
        fresh[p.id] = p
    log.info("built %d Paper objects (%d arxiv lookups failed)",
             len(fresh), len(not_found))

    if not fresh:
        log.info("nothing to import — exit")
        return 0

    # ---- 2. Judge gate --------------------------------------------------
    log.info("running DeepSeek judge on %d papers ...", len(fresh))
    cache = JudgeCache(ROOT / "data" / "judge_cache.json")
    keeps: dict[str, Paper] = {}
    drops: list[tuple[Paper, dict]] = []
    for i, (pid, p) in enumerate(list(fresh.items()), 1):
        cached = cache.get(pid)
        if cached is not None:
            j = cached
        else:
            try:
                j = judge_paper(p.title, p.abstract or p.title)
            except JudgeUnavailable as e:
                log.error("judge unavailable: %s — abort", e)
                return 1
            except Exception as e:
                log.warning("[%d] judge error on %s: %s — keep paper", i, pid, e)
                keeps[pid] = p
                time.sleep(args.pace)
                continue
            cache.put(pid, j)
            time.sleep(args.pace)
        p.judge = {
            "relevant": j.relevant,
            "research_value": j.research_value,
            "primary_channel": j.primary_channel,
            "reason": j.reason,
            "model": j.model,
        }
        log.info("[%3d/%d] judge %s «%s» → %s (%s)", i, len(fresh), pid[:32],
                 p.title[:55], "PASS" if j.relevant else "DROP",
                 j.research_value)
        if j.relevant:
            keeps[pid] = p
        else:
            drops.append((p, p.judge))
    cache.save()
    log.info("judge: %d pass, %d drop", len(keeps), len(drops))

    # ---- 3. Enrich (institutions + method_tags) -------------------------
    if keeps:
        log.info("running DeepSeek enrich on %d papers ...", len(keeps))
        ecache = EnrichCache(ROOT / "data" / "enrich_cache.json")
        for i, (pid, p) in enumerate(keeps.items(), 1):
            cached = ecache.get(pid)
            if cached is not None:
                p.institutions = cached.institutions
                p.method_tags = cached.method_tags
                continue
            try:
                authors_text = "、".join(a.name for a in (p.authors or [])[:8])
                e = enrich_paper(p.title, p.abstract, authors_text)
                ecache.put(pid, e)
                p.institutions = e.institutions
                p.method_tags = e.method_tags
                log.info("[%3d/%d] enrich %s → inst=%s methods=%s",
                         i, len(keeps), pid[:32], e.institutions, e.method_tags)
                time.sleep(args.pace)
            except EnrichUnavailable as ex:
                log.warning("enrich skipped (%s)", ex)
            except Exception as ex:
                log.warning("enrich error on %s: %s", pid, ex)
        ecache.save()

    # ---- 4. Cover + translation + badges --------------------------------
    sources_cfg = cfg.load_sources()
    ctx = build._build_enrichment_context(sources_cfg, keeps)
    existing_full = build._existing_papers()
    build.process_new_papers(keeps, existing_full, ctx)

    # ---- 5. Rewrite feed / digest / rss --------------------------------
    all_papers = list(build._existing_papers().values())
    build.write_feed(all_papers)
    sorted_papers = sorted(all_papers, key=lambda p: (p.published, p.id),
                           reverse=True)
    write_markdown_digest(sorted_papers)
    write_rss(sorted_papers)
    log.info("re-rendered feed: %d papers on site", len(all_papers))

    # ---- 6. Report ------------------------------------------------------
    tmp = ROOT / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "awesome-import-report.md"
    lines = [
        "# awesome_papers 2026 导入报告",
        "",
        f"- 日期: {date.today().isoformat()}",
        f"- 候选 (year={args.year}): **{len(rows)}**",
        f"- 已在站上跳过: **{len(rows) - len(fresh_rows)}**",
        f"- arxiv 拉取成功: **{len(fresh)}**",
        f"- 入围 (judge PASS): **{len(keeps)}**",
        f"- 被砍 (judge DROP): **{len(drops)}**",
        "",
        "## 被砍清单",
        "",
        "| ID | 标题 | 价值 | 砍掉原因 |",
        "|---|---|---|---|",
    ]
    for p, j in drops:
        title = (p.title or "").replace("|", "／")[:70]
        lines.append(
            f"| `{p.arxiv_id}` | {title} | {j.get('research_value','')} | "
            f"{(j.get('reason','') or '').replace('|','／')[:120]} |"
        )
    if not drops:
        lines.append("| — | — | — | *全部通过* |")
    lines.append("")
    lines.append("## arxiv 找不到的")
    lines.append("")
    if not_found:
        for r in not_found:
            lines.append(f"- `{r['arxiv_id']}`  {r['title_hint']}")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 入围清单（按频道）")
    lines.append("")
    from collections import defaultdict
    by_ch = defaultdict(list)
    for pid, p in keeps.items():
        by_ch[p.channels[0] if p.channels else "?"].append(p)
    for ch, papers in by_ch.items():
        lines.append(f"### {ch} ({len(papers)})")
        for p in papers:
            insts = "／".join(p.institutions or []) or "—"
            methods = "／".join(p.method_tags or []) or "—"
            lines.append(
                f"- [{p.arxiv_id}] {p.title[:90]} · 机构: {insts} · 方法: {methods}"
            )
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("report: %s", out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
