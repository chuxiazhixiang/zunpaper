"""Monthly domain digest (P6).

每月一篇 1500 字综述，让 LLM 把"这一整月人形 / 机器人圈干了啥"讲清楚。Scholar
Inbox 没有这种"主编视角"，纯靠 paper 列表自己看；redpaper 把它做出来就是
一个明显高于 Scholar Inbox 的"产品力"层。

输入：site/data/papers/ 一整个月的 Paper JSON（基于 published 月份）。
输出：
  - site/data/digest/monthly/2026-05.json     供前端渲染
  - site/digest/monthly-2026-05.md            mirror 的 markdown（顺手）
  - site/monthly.html 入口页（已经在 templates 里维护）

LLM：复用 enrich.py 同款 DeepSeek-V4-Flash。输入：每篇 paper 的标题 + tldr_zh +
方法 / 平台 / sim 字段（短 ≤2K tokens / 篇 也压不下 100 篇）。

成本估算（DeepSeek V4-Flash）：~1 月 100 篇 paper × 50 tokens/篇 = 5000 input
tokens；output 2000 tokens；按官方报价 ~¥0.01-0.03。可以接受。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests

from . import config as cfg
from .models import Paper

log = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
TIMEOUT = 90


SYSTEM_PROMPT = (
    "你是人形 / 机器人领域月度综述编辑。我会给你一个月内被 Zunpaper 收录的论文 + 视频"
    "清单（每条已经经过 DeepSeek 把关，质量都过关）。请写一份 1500–2000 字的中文综述，"
    "面向已经在做人形 / 机器人研究的读者（不是科普）。\n"
    "\n"
    "结构必须严格按下面 4 段：\n"
    "  ## 一句话\n"
    "    用一句话（≤40 字）概括这个月的最大趋势。\n"
    "  ## 五大主题\n"
    "    选 4–6 个本月的明确技术主题（比如：扩展 Diffusion Policy 到全身控制 / "
    "    teleop 数据规模再上一台阶 / sim2real 用 world model 兜底 / VLA 嫁接到机器人手 ...）。\n"
    "    每个主题：一段 80–150 字，必须提到 ≥2 篇代表性论文（用论文标题，不要 ID）。\n"
    "  ## 值得抄作业的工作\n"
    "    挑 3–5 篇最值得复现 / 最被低估的工作，每篇一句话点评：'值得抄什么'。\n"
    "  ## 还没解决的事\n"
    "    列 3–5 个本月看完后还没解决 / 没人做好的问题。\n"
    "\n"
    "硬要求：\n"
    "  - 中文，干，不要客套和概括废话。\n"
    "  - 提到具体论文 / 方法时引用论文标题；不要瞎编。\n"
    "  - 用 markdown，## 二级标题做分段。\n"
    "  - 1500–2000 字。\n"
    "严格按 JSON 输出（不要 markdown 包裹）：\n"
    "{\n"
    '  "headline":  "一句话标题，10-30 字",\n'
    '  "summary":  "完整的 markdown 综述",\n'
    '  "themes":   ["五大主题里每个的小标题，去掉 ##"]\n'
    "}\n"
)


@dataclass
class MonthlyDigest:
    """月度综述结果。"""
    year_month: str            # "2026-05"
    headline: str              # 一句话
    summary_md: str            # markdown
    themes: list[str] = field(default_factory=list)
    paper_count: int = 0
    paper_ids: list[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    generated_at: str = ""


class MonthlyDigestUnavailable(RuntimeError):
    pass


def _filter_month(papers: list[Paper], year_month: str) -> list[Paper]:
    out = []
    for p in papers:
        pub = p.published or ""
        if not pub or len(pub) < 7:
            continue
        if pub[:7] != year_month:
            continue
        # 「被会议/期刊收录后重新冒泡到首页」的老论文（venue_announced 在更晚的月份）
        # 属于它**原始投稿月**，已在当月综述里覆盖过；不要因为重新露出又被算进
        # venue_announced 所在月的综述。这里按 published 月归档已天然正确，额外
        # 显式排除「announce 月 ≠ published 月」时落到 announce 月的情况，双保险。
        ann = getattr(p, "venue_announced", "") or ""
        if ann and ann[:7] != pub[:7] and ann[:7] == year_month:
            continue
        out.append(p)
    return out


def _format_paper_brief(p: Paper) -> str:
    """把一篇 paper 压缩成 LLM 看的 1 行 + 几个标签。"""
    parts = [f"- 《{p.title_zh or p.title}》"]
    if p.tldr_zh:
        parts.append(p.tldr_zh)
    facets = []
    if p.method_family:
        facets.append(f"方法={p.method_family}")
    if p.platform:
        facets.append("平台=" + "/".join(p.platform[:2]))
    if p.sim_stack:
        facets.append("sim=" + "/".join(p.sim_stack[:2]))
    if p.real_robot == "yes":
        facets.append("真机")
    if facets:
        parts.append("[" + " ".join(facets) + "]")
    return " ".join(parts)[:300]


def _build_user_prompt(year_month: str, papers: list[Paper]) -> str:
    head = f"以下是 Zunpaper 在 {year_month} 月份收录的 {len(papers)} 篇论文 / 视频：\n\n"
    body = "\n".join(_format_paper_brief(p) for p in papers)
    tail = "\n\n请按 system prompt 里的 4 段结构写综述，输出 JSON。"
    return head + body + tail


def generate_monthly_digest(year_month: str, papers: list[Paper], *,
                             model: str = DEFAULT_MODEL) -> MonthlyDigest:
    """对一个月所有 paper 跑一次 LLM 综述。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise MonthlyDigestUnavailable("DEEPSEEK_API_KEY missing")
    month_papers = _filter_month(papers, year_month)
    if not month_papers:
        raise MonthlyDigestUnavailable(f"no papers in {year_month}")

    user_prompt = _build_user_prompt(year_month, month_papers)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        # 综述需要"思考"，温度稍高让生成更顺；但还是要可复现，控制在 0.4。
        "temperature": 0.4,
        "thinking": {"type": "disabled"},
        "max_tokens": 4000,
    }
    r = requests.post(
        DEEPSEEK_URL, json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    data = _parse_response(raw)

    return MonthlyDigest(
        year_month=year_month,
        headline=str(data.get("headline", "")).strip(),
        summary_md=str(data.get("summary", "")).strip(),
        themes=[str(t).strip() for t in (data.get("themes") or [])][:6],
        paper_count=len(month_papers),
        paper_ids=[p.id for p in month_papers],
        model=model,
        generated_at=date.today().isoformat(),
    )


_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _parse_response(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(raw or "")
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except Exception:
            return {}


def write_digest_files(d: MonthlyDigest, repo_root: Path | None = None) -> Path:
    """把综述写到 site/data/digest/monthly/<ym>.json 和 site/digest/monthly-<ym>.md。"""
    root = repo_root or cfg.REPO_ROOT
    json_dir = root / "site" / "data" / "digest" / "monthly"
    md_dir = root / "site" / "digest"
    json_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)

    json_path = json_dir / f"{d.year_month}.json"
    md_path = md_dir / f"monthly-{d.year_month}.md"

    json_path.write_text(
        json.dumps(
            {
                "year_month": d.year_month,
                "headline": d.headline,
                "summary_md": d.summary_md,
                "themes": d.themes,
                "paper_count": d.paper_count,
                "paper_ids": d.paper_ids,
                "model": d.model,
                "generated_at": d.generated_at,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    md_lines = [
        f"# {d.year_month} 月度领域综述",
        "",
        f"_由 Zunpaper（{d.model}）自动生成，覆盖 {d.paper_count} 篇论文 / 视频；{d.generated_at}_",
        "",
        f"**一句话：** {d.headline}",
        "",
        d.summary_md,
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    return json_path


def write_index(digests: list[MonthlyDigest], repo_root: Path | None = None) -> Path:
    """所有已生成月度综述的列表，前端 monthly.html 用。"""
    root = repo_root or cfg.REPO_ROOT
    out = root / "site" / "data" / "digest" / "monthly_index.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    items = [
        {
            "year_month": d.year_month,
            "headline": d.headline,
            "paper_count": d.paper_count,
            "themes": d.themes,
            "generated_at": d.generated_at,
        }
        for d in sorted(digests, key=lambda x: x.year_month, reverse=True)
    ]
    out.write_text(
        json.dumps({"digests": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


__all__ = [
    "MonthlyDigest",
    "MonthlyDigestUnavailable",
    "generate_monthly_digest",
    "write_digest_files",
    "write_index",
]
