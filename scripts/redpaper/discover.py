"""LLM-augmented paper discovery —— 用搜索能力的 LLM 主动找最近相关论文。

为什么要这个：
    主管道 `arxiv_source.fetch_all` 走 「arxiv categories + keywords」召回，
    被 keyword 列表困死 —— 没在 channels.yaml 里写过的新词（新工作命名、
    新平台名）召不回来。这里加一层 LLM 推荐：把最近 N 天的 arxiv 让一个
    带搜索能力的 LLM 去扫，专门补关键词漏召回的论文。

后端选择：
    优先 Gemini-2.0-flash 带 google_search grounding（真实联网，免费层 1500/day），
    没有 GEMINI_API_KEY 时回退 DeepSeek-V4 + 知识推理（不联网，靠模型记忆推断）。

输出：
    list[Paper]，已经验证过 arxiv ID 真实存在、且不在现有数据集里的。可以
    直接合并到 build.py 的 fresh 字典。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

from . import config as cfg
from .models import Paper

log = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={api_key}"
)
GEMINI_MODEL = os.environ.get("REDPAPER_DISCOVER_GEMINI_MODEL", "gemini-2.5-flash")
DEEPSEEK_MODEL = os.environ.get("REDPAPER_DISCOVER_DEEPSEEK_MODEL", "deepseek-chat")
ARXIV_ID_RE = re.compile(r"(\d{4})\.(\d{4,6})")


# ----- Prompt --------------------------------------------------------------

_SYSTEM_PROMPT = """你是一位机器人方向的资深研究员，帮用户从 **真实存在的 arXiv 论文** 里挑出最值得读的。

**严格要求：必须用 Google 搜索 arxiv.org，禁止凭印象编造 arxiv ID**。任何
你不能在搜索结果里直接看到原文链接的论文，不要列出来。

用户关心的 6 个方向（与 redpaper 站点频道对齐）：
  - loco-manip-wbc：人形机器人全身控制 / Loco-Manipulation / Whole-Body Control / Whole-Body VLA / 人形 VLA
  - manipulation：灵巧手 / 抓取 / 双臂 / 桌面操作 / 操作 VLA（OpenVLA / π0 / Octo / RDT / Helix 桌面版 / EgoVLA / Diffusion Policy / ACT）
  - teleop：沉浸式遥操作 / VR / 力反馈 / 主从 / 动捕遥操作
  - locomotion：双足 / 四足运动 / parkour / 地形 / 跳跃
  - sim2real：仿真到真机 / 域随机化 / residual policy / Isaac Sim/Lab
  - world-model：JEPA / V-JEPA / Cosmos / Genie / Dreamer / 视频世界模型 / 物理世界模型 / 4D / Gaussian world model

任务：在 arxiv.org 上搜最近 {days} 天发布或更新的论文，每个方向最多 {per_channel} 篇。

约束：
  - **必须有真实 arXiv 链接**（形如 https://arxiv.org/abs/XXXX.XXXXX 或 https://arxiv.org/pdf/XXXX.XXXXX）；
    把你在搜索结果里看到的完整 URL 复制到 `arxiv_url` 字段
  - **必须是真实标题**，从搜索结果原样照抄，不要意译
  - 只接受 arxiv 大类是 cs.RO / cs.LG / cs.CV / cs.AI / cs.HC / cs.SY / eess.SY 的论文
  - 跳过医疗 / 手术 / 自动驾驶端到端控制 / 公司融资 / 数学 / 物理 / 生物 等无关方向
  - 如果搜不到符合方向的，宁可少给几篇，不要凑数

严格按 JSON 输出（不要 markdown，不要解释）：
{{
  "papers": [
    {{
      "arxiv_url": "<完整 arxiv URL>",
      "title": "<论文真实标题，原样照抄>",
      "primary_channel": "loco-manip-wbc | manipulation | teleop | locomotion | sim2real | world-model",
      "why": "<一句中文 20-40 字，为什么值得看>"
    }}
  ]
}}
"""

# 验证时必须命中的 arxiv 大类（命中任一即可）
_VALID_CATEGORIES = {
    "cs.RO", "cs.LG", "cs.CV", "cs.AI", "cs.HC", "cs.SY", "eess.SY",
    "cs.MA", "cs.NE",
}

_URL_ARXIV_ID_RE = re.compile(
    r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,6})", re.IGNORECASE
)


# ----- Gemini grounded search ----------------------------------------------

def _call_gemini_grounded(days: int, per_channel: int, timeout: float = 90) -> list[dict]:
    """Gemini-2.0/2.5 flash with google_search grounding tool. 真实联网。"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL, api_key=api_key)
    prompt = _SYSTEM_PROMPT.format(days=days, per_channel=per_channel)
    # Gemini's grounding tool comes in two API names depending on model gen:
    #   gemini-1.5: "google_search_retrieval"
    #   gemini-2.0/2.5: "google_search"
    # We default to 2.0+ and let the user override via env if needed.
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.2,
            # 注意：grounding 模式下不能强制 responseMimeType=json，模型必须
            # 走 grounded text → 我们自己 extract JSON。
        },
    }
    r = requests.post(url, json=body, timeout=timeout)
    if r.status_code >= 400:
        log.warning("gemini discover HTTP %d: %s", r.status_code, r.text[:500])
        r.raise_for_status()
    data = r.json()
    # Cherry-pick the model output
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        log.warning("gemini discover: cannot parse response: %s", data)
        raise RuntimeError(f"unexpected gemini response shape: {e}")
    papers = _extract_papers_from_text(text)
    log.info("gemini discover: parsed %d candidates", len(papers))
    return papers


# ----- DeepSeek knowledge fallback -----------------------------------------

def _call_deepseek_knowledge(days: int, per_channel: int, timeout: float = 60) -> list[dict]:
    """DeepSeek-V4 不能联网；纯靠模型训练知识推理。仅作 Gemini 不可用时的
    兜底，对 2026 年最新论文几乎无效（训练截止往往落后几个月）。"""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    prompt = _SYSTEM_PROMPT.format(days=days, per_channel=per_channel)
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "请按上面 JSON 格式给出。"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 2000,
    }
    r = requests.post(
        DEEPSEEK_URL,
        json=body,
        timeout=timeout,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    papers = _extract_papers_from_text(text)
    log.info("deepseek discover (knowledge-only): parsed %d candidates", len(papers))
    return papers


def _extract_papers_from_text(text: str) -> list[dict]:
    """LLM 输出可能含 markdown / grounding citations，提取出 JSON 列表。"""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE)
    # Find the outermost {...}
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return []
    return obj.get("papers", []) if isinstance(obj, dict) else []


# ----- arxiv validation ----------------------------------------------------

def _validate_and_fetch(
    candidates: list[dict],
    channels: list[cfg.Channel],
    existing_ids: set[str],
    max_age_days: int = 30,
) -> list[Paper]:
    """对每个候选 arxiv_id 调 arxiv API 验证 + 拉摘要 + 包成 Paper。
    丢掉假 ID（hallucination）、已在数据集、年龄超 max_age_days 的论文。"""
    import arxiv  # 已经在 requirements

    fetched: list[Paper] = []
    seen_ids: set[str] = set()
    client = arxiv.Client(page_size=1, delay_seconds=3, num_retries=2)
    valid_channel_ids = {c.id for c in channels}
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=max_age_days)

    for c in candidates:
        # 优先吃 LLM 给的真实 URL；URL 解析不到再尝试 arxiv_id 字段
        candidate_url = (c.get("arxiv_url") or "").strip()
        m = _URL_ARXIV_ID_RE.search(candidate_url)
        if not m:
            raw_id = (c.get("arxiv_id") or "").strip()
            m = ARXIV_ID_RE.search(raw_id)
            if not m:
                continue
            clean_id = f"{m.group(1)}.{m.group(2)}"
        else:
            clean_id = m.group(1)

        if clean_id in seen_ids:
            continue
        seen_ids.add(clean_id)
        slug = f"arxiv-{clean_id.replace('.', '-')}"
        if slug in existing_ids or clean_id in existing_ids:
            log.debug("discover: %s already in dataset", clean_id)
            continue

        try:
            search = arxiv.Search(id_list=[clean_id])
            result = next(iter(client.results(search)), None)
        except Exception as e:
            log.warning("discover: arxiv lookup for %s failed: %s", clean_id, e)
            continue
        if not result:
            log.info("discover: arxiv ID %s not found", clean_id)
            continue

        # 真实性二次校验：标题应大致匹配 LLM 报的标题（防对错 ID 真论文）
        llm_title = (c.get("title") or "").strip().lower()
        real_title = (result.title or "").strip()
        if llm_title and real_title:
            # 简单字符串相似度：LLM 标题里 ≥ 50% 的 token 出现在真实标题里
            llm_tokens = set(re.findall(r"\w+", llm_title))
            real_tokens = set(re.findall(r"\w+", real_title.lower()))
            if llm_tokens:
                overlap = len(llm_tokens & real_tokens) / len(llm_tokens)
                if overlap < 0.3:
                    log.info(
                        "discover: title mismatch for %s — LLM «%s» vs real «%s» (overlap %.0f%%) → skip",
                        clean_id, llm_title[:50], real_title[:50], overlap * 100,
                    )
                    continue

        # Category 强制校验：arxiv 大类必须命中具身/AI/控制大类，过滤掉数学
        # / 物理 / 生物等 LLM 偶尔混进来的胡话
        cats = set(getattr(result, "categories", []) or [])
        if not (cats & _VALID_CATEGORIES):
            log.info(
                "discover: %s categories=%s not in valid set → skip",
                clean_id, cats,
            )
            continue

        # 年龄校验：LLM 偶尔会给 6 个月前的"老熟人"论文，超 max_age_days 砍掉。
        # 用 published 而不是 updated，因为 updated 经常被作者改版日期顶到最新。
        pub_dt = result.published.date() if result.published else None
        if pub_dt and pub_dt < cutoff:
            log.info(
                "discover: %s published %s older than cutoff %s → skip",
                clean_id, pub_dt, cutoff,
            )
            continue

        primary = (c.get("primary_channel") or "").strip().lower()
        ch_list = [primary] if primary in valid_channel_ids else []
        paper = Paper(
            id=slug,
            arxiv_id=clean_id,
            source="arxiv_discover",
            source_tags=["llm_discover"],
            title=real_title,
            abstract=(result.summary or "").strip(),
            authors=[],
            categories=list(cats),
            published=result.published.date().isoformat() if result.published else "",
            updated=result.updated.date().isoformat() if result.updated else "",
            abs_url=getattr(result, "entry_id", "") or f"https://arxiv.org/abs/{clean_id}",
            pdf_url=getattr(result, "pdf_url", "") or f"https://arxiv.org/pdf/{clean_id}",
            channels=ch_list,
        )
        try:
            from .models import Author
            paper.authors = [Author(name=a.name) for a in (result.authors or [])]
        except Exception:
            pass
        fetched.append(paper)
        log.info("discover: ✅ %s «%s»", clean_id, real_title[:60])
        time.sleep(0.4)
    return fetched


# ----- Public API ----------------------------------------------------------

def discover_recent_papers(
    channels: list[cfg.Channel],
    existing_ids: set[str],
    days: int = 14,
    per_channel: int = 5,
) -> list[Paper]:
    """Top-level entry：用搜索能力的 LLM 找最近 N 天的高质量论文，验证后返回。

    优先 Gemini grounded search（真实联网），失败回退 DeepSeek 知识推理。
    返回的 Paper 已经验证过 arxiv ID 真实，且不在 existing_ids 集合里。
    """
    candidates: list[dict] = []
    if os.environ.get("GEMINI_API_KEY"):
        try:
            candidates = _call_gemini_grounded(days, per_channel)
        except Exception as e:
            log.warning("gemini discover failed (%s); falling back to deepseek", e)
    if not candidates and os.environ.get("DEEPSEEK_API_KEY"):
        try:
            candidates = _call_deepseek_knowledge(days, per_channel)
        except Exception as e:
            log.warning("deepseek discover failed (%s); giving up", e)
    if not candidates:
        return []
    log.info("discover: %d LLM candidates, validating against arxiv...", len(candidates))
    # 允许 LLM 找到 30 天内的论文（即使让它限定 14 天，超出一点也 OK；
    # 关键是不要"半年前的老熟人"）。
    papers = _validate_and_fetch(
        candidates, channels, existing_ids, max_age_days=max(days * 2, 30),
    )
    log.info("discover: %d validated", len(papers))
    return papers


# ----- CLI test ------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    channels = cfg.load_channels()
    # In CLI mode, use existing on-disk papers as the dedup set.
    existing: set[str] = set()
    for jp in cfg.PAPERS_DIR.glob("arxiv-*.json"):
        existing.add(jp.stem)
    out = discover_recent_papers(channels, existing, days=14, per_channel=5)
    print(f"\nDiscovered {len(out)} new papers:")
    for p in out:
        print(f"  {p.arxiv_id}  {p.title[:70]}  → {p.channels}")
