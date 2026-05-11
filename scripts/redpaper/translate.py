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


SYSTEM_PROMPT = (
    "你是一名 AI 顶会论文编辑，把英文论文的标题和摘要翻译成自然的中文，"
    "并产出一句不超过 30 字的中文 TL;DR。"
    "严格输出 JSON：{\"title_zh\": \"...\", \"abstract_zh\": \"...\", \"tldr_zh\": \"...\"}，"
    "不要添加额外说明、不要使用 Markdown 代码块。"
    "翻译要求：保留专有名词（如 LLM、Transformer、RLHF、CLIP）原文，"
    "公式或符号原样保留，语言简洁自然。"
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
    return Translation(
        title_zh=title,
        abstract_zh=abstract,
        tldr_zh=abstract[:60].replace("\n", " ").strip(),
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
    """Helper that retries transient errors with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return translate(title, abstract)
        except Exception as e:  # pragma: no cover
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    log.error("translate failed after %d attempts: %s", retries + 1, last_exc)
    return _dryrun(title, abstract)
