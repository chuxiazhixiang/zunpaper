"""LLM-backed translator for paper titles / abstracts / TL;DRs.

Multiple backends are supported, chosen via the environment variable
configured in `site.yaml` (default REDPAPER_LLM_BACKEND):

  - dryrun    no API call; falls back to English text. Useful for local dev.
  - gemini    Google Gemini (free tier works well).  Needs GEMINI_API_KEY.
  - deepseek  DeepSeek chat API (OpenAI-compatible).  Needs DEEPSEEK_API_KEY.
  - openai    Any OpenAI-compatible endpoint.        Needs OPENAI_API_KEY
              and optionally OPENAI_BASE_URL / OPENAI_MODEL.

All backends share a single JSON-output prompt so the consumer just needs to
parse one structure. Translations are cached at the per-paper level via the
caller (see `build.py`).
"""
from __future__ import annotations

import json
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)


@dataclass
class Translation:
    title_zh: str = ""
    abstract_zh: str = ""
    tldr_zh: str = ""
    cover_zh: str = ""


SYSTEM_PROMPT = (
    "你是一名 AI 顶会论文编辑，要把英文论文的标题和摘要翻译成自然的中文，"
    "并额外产出两段中文短文案。\n"
    "严格输出 JSON，4 个字段：\n"
    "{\n"
    '  "title_zh": "中文标题（直译，专有名词如 LLM/Transformer/RLHF/CLIP 保留原文）",\n'
    '  "abstract_zh": "中文摘要（自然流畅，公式符号原样保留）",\n'
    '  "tldr_zh": "30 字以内、不带 emoji 的客观一句话总结",\n'
    '  "cover_zh": "20-35 字的小红书风格封面文案，要抓眼球、像爆款帖子标题"\n'
    "}\n"
    "cover_zh 风格参考（仅仅是节奏和语气示例，禁止照抄数字 / 系统名）：\n"
    " - “终于不用手工调参｜这个新框架让推理直接起飞”\n"
    " - “你以为它会失败？结果在数学题上反杀人类基线”\n"
    " - “扩散模型还能这么用？保真度还更高”\n"
    "硬性规则：\n"
    " - 必须基于原文事实，不能虚构\n"
    " - 不要捏造任何具体百分比、节省比例、加速倍数或排名；除非原文摘要里明确出现，否则不要写“99%”“2 倍”“SOTA”这类数字\n"
    " - 系统 / 模型 / 数据集名只能用原文中真实出现的（如 AutoTTS、CLIP-2）；不要造一个新名字\n"
    " - 突出最反直觉、最让人想点进来读的一个点\n"
    " - 可以用 ｜ ? ！ 等标点，最多 2 个 emoji，最好 0 个\n"
    " - 不要使用 Markdown 代码块"
)

USER_PROMPT_TEMPLATE = textwrap.dedent(
    """\
    论文标题（英文）：
    {title}

    论文摘要（英文）：
    {abstract}
    """
)


def translate(title: str, abstract: str) -> Translation:
    backend = _resolve_backend()
    fn = _BACKENDS.get(backend, _dryrun)
    try:
        return fn(title, abstract)
    except Exception as e:
        log.warning("LLM backend %s failed: %s; falling back to dryrun", backend, e)
        return _dryrun(title, abstract)


def _resolve_backend() -> str:
    # Lazy import to avoid circular references on package load.
    from .config import load_site
    site = load_site()
    env_name = site.translation_backend_env
    return os.environ.get(env_name, site.translation_default_backend).lower()


def _extract_json(text: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from an LLM response."""
    if not text:
        return {}
    text = text.strip()
    # Strip ```json ... ``` fences if present
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Last resort: find the first { ... } block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}
    return {}


def _dryrun(title: str, abstract: str) -> Translation:
    """No-op backend that just echoes English back. Lets local devs render the
    site without an API key."""
    short = abstract[:60].replace("\n", " ").strip()
    return Translation(
        title_zh=title,
        abstract_zh=abstract,
        tldr_zh=short,
        cover_zh=short,
    )


# -- Gemini -----------------------------------------------------------------

def _gemini(title: str, abstract: str) -> Translation:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [
                {"text": USER_PROMPT_TEMPLATE.format(title=title, abstract=abstract)}
            ]}
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }
    r = requests.post(url, json=body, timeout=60)
    r.raise_for_status()
    data = r.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    obj = _extract_json(text)
    return Translation(
        title_zh=obj.get("title_zh", "").strip(),
        abstract_zh=obj.get("abstract_zh", "").strip(),
        tldr_zh=obj.get("tldr_zh", "").strip(),
        cover_zh=obj.get("cover_zh", "").strip(),
    )


# -- DeepSeek / OpenAI-compatible -------------------------------------------

def _openai_compat(title: str, abstract: str, base_url: str, api_key: str, model: str) -> Translation:
    url = base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(title=title, abstract=abstract)},
        ],
    }
    r = requests.post(
        url,
        json=body,
        timeout=60,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    obj = _extract_json(text)
    return Translation(
        title_zh=obj.get("title_zh", "").strip(),
        abstract_zh=obj.get("abstract_zh", "").strip(),
        tldr_zh=obj.get("tldr_zh", "").strip(),
        cover_zh=obj.get("cover_zh", "").strip(),
    )


def _deepseek(title: str, abstract: str) -> Translation:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    return _openai_compat(title, abstract, base_url, api_key, model)


def _openai(title: str, abstract: str) -> Translation:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    return _openai_compat(title, abstract, base_url, api_key, model)


_BACKENDS: dict[str, Callable[[str, str], Translation]] = {
    "dryrun": _dryrun,
    "gemini": _gemini,
    "deepseek": _deepseek,
    "openai": _openai,
}


def translate_with_retry(title: str, abstract: str, retries: int = 2, backoff: float = 2.0) -> Translation:
    """Helper that retries transient errors with exponential backoff.

    Some backends (notably DeepSeek on math-heavy abstracts) occasionally
    return valid JSON with all empty fields — JSON mode strips invalid
    output instead of erroring. Treat that as a soft failure and retry
    instead of silently falling back to English text.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            t = translate(title, abstract)
            if t.title_zh and t.cover_zh:
                return t
            log.warning(
                "translate attempt %d returned empty fields (title_zh=%r cover_zh=%r); retrying",
                attempt + 1, t.title_zh, t.cover_zh,
            )
        except Exception as e:  # pragma: no cover
            last_exc = e
            log.warning("translate attempt %d errored: %s", attempt + 1, e)
        if attempt < retries:
            time.sleep(backoff * (2 ** attempt))
    log.error(
        "translate failed after %d attempts (last_exc=%s); using dryrun fallback",
        retries + 1, last_exc,
    )
    return _dryrun(title, abstract)
