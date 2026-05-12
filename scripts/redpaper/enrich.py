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
from dataclasses import dataclass, asdict, field
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("REDPAPER_ENRICH_MODEL", "deepseek-v4-flash")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
ENRICH_TIMEOUT = 60

SYSTEM_PROMPT = (
    "你是人形机器人论文标签助手。读一篇 paper 的标题 + 摘要 + 作者，输出 7 组结构化字段。\n"
    "\n"
    "(1) institutions：参与单位（公司或大学），从作者署名 / 摘要 / 项目名里推断。\n"
    "    - 必须真实存在，不要瞎编。最多 3 个，按相关度排序。\n"
    "    - 例：MIT CSAIL / Stanford / Tsinghua / 清华 / 北大 / SJTU / CUHK / 上海AI Lab /\n"
    "      Boston Dynamics / Figure / 1X / Tesla / 宇树 / 智元 / 银河通用 / Physical Intelligence /\n"
    "      Skild AI / Generalist / NVIDIA / Google DeepMind ...\n"
    "    - 拿不准 → 留空 []。\n"
    "\n"
    "(2) method_tags：方法 + 问题混合标签，2-3 个，少而精。\n"
    "    - 方法侧例：RL / Imitation Learning / DAgger / VAE / Diffusion Policy / ACT /\n"
    "      VLA / World Model / MPC / Curriculum / Domain Randomization / Foundation Model /\n"
    "      GAIL / Behavior Cloning / Test-Time Training / Distillation ...\n"
    "    - 问题侧例：sim-to-real / long-horizon / fall recovery / parkour / 灵巧抓取 /\n"
    "      双臂协调 / 跨本体迁移 / 视觉运动控制 / 特技动作 / 动作重定向 / 真机部署 ...\n"
    "    - 中英文都行。\n"
    "\n"
    "(3) platform：用了哪些机器人硬件平台。最多 3 个。\n"
    "    - 例：Unitree G1 / Unitree H1 / Booster T1 / Atlas / Figure 02 / GR-1 / GR-2 /\n"
    "      Apollo / ANYmal / Spot / Cassie / Digit / ALOHA / Franka / Reachy / ...\n"
    "    - 没在标题/摘要里提到具体型号 → 留空 []，不要硬猜。\n"
    "\n"
    "(4) sim_stack：用了哪些仿真器 / 仿真栈。最多 2 个。\n"
    "    - 例：Isaac Lab / Isaac Sim / Isaac Gym / MuJoCo / Genesis / GenSim / RoboCasa /\n"
    "      RoboTwin / SAPIEN / Habitat / DRAKE / PyBullet / Unity / ...\n"
    "    - 没明确说 → 留空 []。\n"
    "\n"
    "(5) method_family：主方法家族，单选一个，从下列里挑（实在不属于任一就 \"\"）：\n"
    "    \"RL\" | \"IL\" | \"VLA\" | \"MPC\" | \"Diffusion\" | \"WorldModel\" | \"Foundation\" |\n"
    "    \"Hybrid\" | \"Hardware\" | \"Dataset\" | \"Benchmark\" | \"Survey\" | \"\"\n"
    "\n"
    "(6) real_robot：这篇有没有真机实验。\n"
    "    \"yes\" = 摘要明确说在真机器人上验证 / 有真机视频；\n"
    "    \"no\"  = 明确只是仿真 / 数据集 / 方法论文；\n"
    "    \"\"   = 拿不准。\n"
    "\n"
    "(7) training_summary：训练规模 / 数据量的一句话摘要，0-25 字。\n"
    "    - 例：\"30K env-steps × 5 GPU-days\" / \"100K human demos\" / \"1.2M motion clips\" /\n"
    "      \"6h teleop data\" / \"4-stage curriculum\" / \"3000 SE(3) trajectories\"\n"
    "    - 摘要没透露规模 → 留空 \"\"。\n"
    "\n"
    "严格按 JSON 输出（不要 markdown 包裹）：\n"
    "{\n"
    '  "institutions": [],\n'
    '  "method_tags": [],\n'
    '  "platform": [],\n'
    '  "sim_stack": [],\n'
    '  "method_family": "",\n'
    '  "real_robot": "",\n'
    '  "training_summary": ""\n'
    "}\n"
)


_METHOD_FAMILIES = {
    "RL", "IL", "VLA", "MPC", "Diffusion", "WorldModel", "Foundation",
    "Hybrid", "Hardware", "Dataset", "Benchmark", "Survey", "",
}
_REAL_ROBOT = {"yes", "no", ""}


@dataclass
class Enrichment:
    institutions: list[str]
    method_tags: list[str]
    # P1: 领域专属结构化字段
    platform: list[str] = field(default_factory=list)
    sim_stack: list[str] = field(default_factory=list)
    method_family: str = ""
    real_robot: str = ""
    training_summary: str = ""
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
        # 输出空间：7 个字段（4 list + 3 string），留 600 token 防截断。
        "max_tokens": 600,
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
    platform = _clean_list(data.get("platform", []))[:3]
    sim_stack = _clean_list(data.get("sim_stack", []))[:2]
    mf = str(data.get("method_family", "") or "").strip()
    if mf not in _METHOD_FAMILIES:
        mf = ""
    rr = str(data.get("real_robot", "") or "").strip().lower()
    if rr not in _REAL_ROBOT:
        rr = ""
    ts = str(data.get("training_summary", "") or "").strip()[:80]
    return Enrichment(
        institutions=insts,
        method_tags=methods,
        platform=platform,
        sim_stack=sim_stack,
        method_family=mf,
        real_robot=rr,
        training_summary=ts,
        model=model,
    )


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
        # 旧 cache 不一定有新字段；都用 .get(...) 容错。
        return Enrichment(
            institutions=list(e.get("institutions") or []),
            method_tags=list(e.get("method_tags") or []),
            platform=list(e.get("platform") or []),
            sim_stack=list(e.get("sim_stack") or []),
            method_family=str(e.get("method_family") or ""),
            real_robot=str(e.get("real_robot") or ""),
            training_summary=str(e.get("training_summary") or ""),
            model=e.get("model", ""),
        )

    def has_deep_fields(self, pid: str) -> bool:
        """老缓存只有 institutions+method_tags，新字段缺失则需重抽。"""
        e = self.entries.get(pid)
        if not e:
            return False
        # 只要任何一个新字段已经存在（即使是空字符串/空列表）就算抽过
        return any(k in e for k in ("platform", "sim_stack", "method_family"))

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
