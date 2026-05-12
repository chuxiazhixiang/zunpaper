"""DeepSeek-V4-Flash 卡片二级标签提取 —— 机构 + 方法 / 问题 tag。

Why：
    站长想在主页每张卡片下面看到两组 chip：
      (1) 机构（公司或大学）：MIT CSAIL / Boston Dynamics / 宇树 / 智元 ...
      (2) 方法 + 问题：DAgger / VAE / sim2real / 特技动作 / ...
    这两个信息靠关键词匹配抽不准（机构名变体太多、方法 tag 又千奇百怪），
    所以让 LLM 读 abstract 抽 2-3 个出来。

Why V4-Flash:
    跟 judge 同款，简单的信息抽取分类任务，V4-Flash 完全够用且便宜。
    单篇 ~400 token in + ~100 token out ≈ ¥0.0006/篇，全站 100 篇也就
    ¥0.06。

Cache:
    data/enrich_cache.json （仓库根，不暴露给 Pages）。判定结果跟着
    paper.id 缓存，下次 build 不重复付费。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("REDPAPER_ENRICH_MODEL", "deepseek-v4-flash")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
ENRICH_TIMEOUT = 60

SYSTEM_PROMPT = (
    "你是一个机器人论文标签助手。读一篇 paper 的标题 + 摘要，给我两组短标签：\n"
    "  (1) institutions：参与单位（公司或大学），从作者署名 / 摘要 / 项目名里推断。\n"
    "       - 必须是真实存在的机构名，不要瞎编。\n"
    "       - 大学用通用简称：MIT CSAIL / Stanford / Tsinghua / 清华 / 北大 / SJTU / CUHK ...\n"
    "       - 公司用品牌名：Boston Dynamics / Figure / 1X / Tesla / 宇树 / 智元 / 银河通用 / "
    "Physical Intelligence / Skild AI / Generalist / NVIDIA / Google DeepMind ...\n"
    "       - 中文实验室 / 公司可以用中文。最多 3 个，按相关度排序。\n"
    "       - 拿不准 → 留空，不要硬猜。\n"
    "  (2) method_tags：这篇 paper 用的「方法」+ 「解决的问题」标签。\n"
    "       - 方法侧：RL / Imitation Learning / DAgger / VAE / Diffusion Policy / "
    "ACT / VLA / World Model / MPC / Adversarial Training / Curriculum / "
    "Domain Randomization / Self-supervised / Foundation Model / GAIL / IRL / "
    "Behavior Cloning / Test-Time Training / Distillation ...\n"
    "       - 问题侧：sim-to-real / long-horizon / fall recovery / parkour / "
    "灵巧抓取 / 双臂协调 / 跨本体迁移 / 视觉运动控制 / 特技动作 / 长时序操作 / "
    "动作重定向 / 仿真训练 / 真机部署 / 接触丰富操作 / ...\n"
    "       - 一般 2-3 个，少而精。\n"
    "       - 中文 / 英文都行，跟摘要风格保持一致。\n"
    "严格按 JSON 输出（不要任何 markdown 包裹），schema：\n"
    "{\n"
    '  "institutions": ["...", "...", "..."],\n'
    '  "method_tags":  ["...", "...", "..."]\n'
    "}\n"
    "两个列表都最多 3 项；可以为空 `[]`。"
)


@dataclass
class Enrichment:
    institutions: list[str]
    method_tags: list[str]
    model: str = ""


class EnrichUnavailable(RuntimeError):
    pass


def enrich_paper(title: str, abstract: str, authors_text: str = "",
                 *, model: str = DEFAULT_MODEL, api_key: str | None = None,
                 timeout: float = ENRICH_TIMEOUT) -> Enrichment:
    if os.environ.get("REDPAPER_ENRICH_DISABLE") == "1":
        raise EnrichUnavailable("REDPAPER_ENRICH_DISABLE=1")
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise EnrichUnavailable("DEEPSEEK_API_KEY not set")

    user_parts = [f"标题：{title.strip()}"]
    if authors_text.strip():
        user_parts.append(f"作者：{authors_text.strip()[:400]}")
    user_parts.append(f"摘要 / 描述：\n{(abstract or '').strip()[:3000]}")
    user_msg = "\n\n".join(user_parts)

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        # 关掉 reasoning：单纯抽取任务用不上 CoT，开了会吃 max_tokens 配额。
        "thinking": {"type": "disabled"},
        # 输出空间：两个列表 + 中文/英文，留宽一些防截断。
        "max_tokens": 400,
    }
    r = requests.post(DEEPSEEK_URL, json=payload, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }, timeout=timeout)
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    data = _parse_response(raw)

    insts = _clean_list(data.get("institutions", []))[:3]
    methods = _clean_list(data.get("method_tags", []))[:3]
    return Enrichment(institutions=insts, method_tags=methods, model=model)


def _parse_response(raw: str) -> dict:
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


def _clean_list(items) -> list[str]:
    """JSON 字段防御：去重 + 去空 + 转 str + trim。"""
    if not isinstance(items, list):
        return []
    seen = set()
    out: list[str] = []
    for x in items:
        s = str(x or "").strip()
        if not s or s.lower() in {"none", "n/a", "null"}:
            continue
        # 去重（按 lowercase 比较，但保留原大小写）
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


# ---- Cache ---------------------------------------------------------------

class EnrichCache:
    VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self._data = {"version": self.VERSION, "model": DEFAULT_MODEL, "entries": {}}
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                log.warning("enrich cache load failed (%s); starting fresh", e)

    @property
    def entries(self) -> dict[str, dict]:
        return self._data.setdefault("entries", {})

    def get(self, pid: str) -> Enrichment | None:
        e = self.entries.get(pid)
        if not e:
            return None
        return Enrichment(
            institutions=list(e.get("institutions") or []),
            method_tags=list(e.get("method_tags") or []),
            model=e.get("model", ""),
        )

    def put(self, pid: str, j: Enrichment) -> None:
        d = asdict(j)
        d["ts"] = int(time.time())
        self.entries[pid] = d

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
