"""Generate monthly LLM digest(s) for redpaper.

用法：
  # 生成当月（基于今天日期）
  DEEPSEEK_API_KEY=sk-... python scripts/run_monthly_digest.py

  # 指定月份
  DEEPSEEK_API_KEY=sk-... python scripts/run_monthly_digest.py --month 2026-04

  # 全量回填：所有出现过 paper 的月份都生成一次
  DEEPSEEK_API_KEY=sk-... python scripts/run_monthly_digest.py --all
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from redpaper import config as cfg                                # noqa: E402
from redpaper.models import Paper                                 # noqa: E402
from redpaper.monthly_digest import (                             # noqa: E402
    MonthlyDigestUnavailable,
    generate_monthly_digest,
    write_digest_files,
    write_index,
    MonthlyDigest,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("monthly")


def _load_papers() -> list[Paper]:
    papers_dir = ROOT / "site" / "data" / "papers"
    out: list[Paper] = []
    for fp in sorted(papers_dir.glob("*.json")):
        try:
            out.append(Paper.from_dict(json.loads(fp.read_text("utf-8"))))
        except Exception as e:
            log.warning("skip %s: %s", fp.name, e)
    return out


def _collect_months(papers: list[Paper]) -> list[str]:
    c: Counter[str] = Counter()
    for p in papers:
        if p.published and len(p.published) >= 7:
            c[p.published[:7]] += 1
    # 只挑至少 5 篇 paper 的月份，避免月初就开始生成空综述
    return sorted(m for m, n in c.items() if n >= 5)


def _load_existing_digests() -> list[MonthlyDigest]:
    json_dir = ROOT / "site" / "data" / "digest" / "monthly"
    if not json_dir.exists():
        return []
    out: list[MonthlyDigest] = []
    for fp in sorted(json_dir.glob("*.json")):
        try:
            j = json.loads(fp.read_text("utf-8"))
            out.append(MonthlyDigest(
                year_month=j.get("year_month", ""),
                headline=j.get("headline", ""),
                summary_md=j.get("summary_md", ""),
                themes=j.get("themes") or [],
                paper_count=j.get("paper_count", 0),
                paper_ids=j.get("paper_ids") or [],
                model=j.get("model", ""),
                generated_at=j.get("generated_at", ""),
            ))
        except Exception as e:
            log.warning("skip %s: %s", fp.name, e)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", default=None, help="YYYY-MM；不传则用当前月")
    ap.add_argument("--all", action="store_true", help="所有出现过 paper 的月份")
    ap.add_argument("--force", action="store_true", help="即使已有，也重新生成")
    args = ap.parse_args()

    papers = _load_papers()
    log.info("loaded %d papers", len(papers))

    months = []
    if args.all:
        months = _collect_months(papers)
    elif args.month:
        months = [args.month]
    else:
        months = [date.today().strftime("%Y-%m")]

    existing = {d.year_month: d for d in _load_existing_digests()}

    new_digests: list[MonthlyDigest] = []
    for m in months:
        if m in existing and not args.force:
            log.info("skip %s (already generated; pass --force to override)", m)
            continue
        log.info("generating digest for %s ...", m)
        try:
            d = generate_monthly_digest(m, papers)
        except MonthlyDigestUnavailable as e:
            log.warning("digest unavailable for %s: %s", m, e)
            continue
        except Exception as e:
            log.warning("digest failed for %s: %s", m, e)
            continue
        path = write_digest_files(d)
        log.info("  wrote %s (%d papers, %d themes)", path, d.paper_count, len(d.themes))
        existing[m] = d
        new_digests.append(d)

    # 总索引文件（前端 monthly.html 用）
    idx_path = write_index(list(existing.values()))
    log.info("wrote index: %s (%d months)", idx_path, len(existing))


if __name__ == "__main__":
    main()
