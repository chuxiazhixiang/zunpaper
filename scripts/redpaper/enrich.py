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
    "你是人形机器人论文标签助手。我会给你一篇 paper 的【标题 + 作者 + 摘要 + PDF 首页文本】，"
    "你只能依据这些**提供的文本**抽取 5 组结构化字段。\n"
    "\n"
    "★★ 最重要的铁律：只填**文本里明确出现**的信息。绝不根据作者名字、研究主题、\n"
    "   或常见组合去猜。拿不准、文本没写 → 一律留空（[] 或 \"\"）。宁可空着，也不要填错。\n"
    "\n"
    "(1) institutions：作者所属单位（公司 / 大学 / 实验室）。\n"
    "    - **只从 PDF 首页文本里的单位脚注 / 作者署名块识别**（摘要里通常没有单位信息）。\n"
    "    - 原文怎么写就怎么填（可用通用简称：MIT CSAIL / Stanford / 清华 / 上海交大 /\n"
    "      TeleAI / 中国电信 / 复旦 / NVIDIA / 宇树 ...），但**必须文本里真的出现过该单位**。\n"
    "    - ❌ 禁止：看到作者是某知名人士就脑补他现在的单位；看到是人形论文就猜“上海AI Lab/CUHK”。\n"
    "    - PDF 文本里找不到任何单位 → 留空 []。最多 4 个，按出现顺序。\n"
    "\n"
    "(2) method_tags：方法 + 问题混合标签，2-3 个，少而精，必须有文本依据。\n"
    "    - 方法侧例：RL / Imitation Learning / Diffusion Policy / ACT / VLA / World Model /\n"
    "      MPC / Domain Randomization / Behavior Cloning / Distillation ...\n"
    "    - 问题侧例：sim-to-real / long-horizon / loco-manipulation / 灵巧抓取 / 全身控制 /\n"
    "      跨本体迁移 / 视觉运动控制 / 动作重定向 ...\n"
    "    - 中英文都行。文本没体现的方法不要写。\n"
    "\n"
    "(3) platform：用到的机器人硬件平台。最多 3 个。\n"
    "    - **只有文本明确出现具体型号才填**：Unitree G1 / Unitree H1 / Booster T1 / Atlas /\n"
    "      Figure 02 / GR-1 / Apollo / ANYmal / Spot / Cassie / Digit / ALOHA / Franka ...\n"
    "    - ❗只说 “humanoid / quadruped / a robot” 没给型号 → 留空 []。\n"
    "    - ❗注意区分 G1 与 H1 等型号，以文本为准，不要混。\n"
    "\n"
    "(4) sim_stack：用到的仿真器 / 仿真栈。最多 2 个。\n"
    "    - 例：Isaac Lab / Isaac Sim / Isaac Gym / MuJoCo / Genesis / SAPIEN / Habitat /\n"
    "      PyBullet / Gazebo / RoboTwin ...\n"
    "    - 文本没明确提到 → 留空 []。\n"
    "\n"
    "(5) real_robot：是否在真实机器人上做过实验。\n"
    "    \"yes\" = 文本明确说在真机上验证 / 部署 / 有真机实验；\n"
    "    \"no\"  = 文本明确说只是仿真 / 数据集 / 纯方法；\n"
    "    \"\"   = 文本没说清。\n"
    "\n"
    "严格按 JSON 输出（不要 markdown 包裹）：\n"
    "{\n"
    '  "institutions": [],\n'
    '  "method_tags": [],\n'
    '  "platform": [],\n'
    '  "sim_stack": [],\n'
    '  "real_robot": ""\n'
    "}\n"
)

# Reviewer（第二个 AI）：对照原文核对草稿，删掉没有依据的值。
REVIEW_PROMPT = (
    "你是严格的事实核查员。我会给你一篇 paper 的【原始文本】（标题 + 作者 + 摘要 + PDF 首页）"
    "和上一轮助手抽取的【字段草稿】。请逐字段核对：\n"
    "  - 某个值在原始文本里有明确依据 → 保留。\n"
    "  - 找不到依据 / 疑似根据作者名字或研究主题猜的 → 删除（list 删该项，字符串置空）。\n"
    "  - institutions 尤其严格：该单位名必须在【PDF 首页文本】里真实出现，否则删。\n"
    "  - platform 型号（G1 / H1 等）必须文本明确写到，且型号要对；存疑就删。\n"
    "不要新增草稿里没有的值，只做删除 / 收紧。输出修正后的同 schema JSON（institutions /\n"
    "method_tags / platform / sim_stack / real_robot），不要解释、不要 markdown。\n"
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
    # reviewer 是否成功核验过。False = reviewer 调用失败、走了保守降级（已清空
    # 高风险事实字段），缓存标记后下次重试。
    review_ok: bool = True


class EnrichUnavailable(RuntimeError):
    pass


def _deepseek_json(system: str, user: str, key: str, model: str, timeout: float) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "thinking": {"type": "disabled"},
        "max_tokens": 500,
    }
    r = requests.post(DEEPSEEK_URL, json=payload, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }, timeout=timeout)
    r.raise_for_status()
    return _parse_response(r.json()["choices"][0]["message"]["content"])


def _coerce(data: dict, model: str) -> Enrichment:
    rr = str(data.get("real_robot", "") or "").strip().lower()
    if rr not in _REAL_ROBOT:
        rr = ""
    return Enrichment(
        institutions=_clean_list(data.get("institutions", []))[:4],
        method_tags=_clean_list(data.get("method_tags", []))[:3],
        platform=_clean_list(data.get("platform", []))[:3],
        sim_stack=_clean_list(data.get("sim_stack", []))[:2],
        real_robot=rr,
        # method_family / training_summary 已弃用（常含糊无用），固定留空。
        method_family="",
        training_summary="",
        model=model,
    )


def enrich_paper(title: str, abstract: str, authors_text: str = "", pdf_text: str = "",
                 *, model: str = DEFAULT_MODEL, api_key: str | None = None,
                 timeout: float = ENRICH_TIMEOUT, review: bool = True) -> Enrichment:
    """两步抽取：①writer 依据文本抽字段（默认弃权，不准猜）→ ②reviewer 对照原文
    核对、删掉没依据的值。pdf_text = PDF 首页文本（含真实单位脚注 / 平台型号）。"""
    if os.environ.get("REDPAPER_ENRICH_DISABLE") == "1":
        raise EnrichUnavailable("REDPAPER_ENRICH_DISABLE=1")
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise EnrichUnavailable("DEEPSEEK_API_KEY not set")

    src_parts = [f"标题：{title.strip()}"]
    if authors_text.strip():
        src_parts.append(f"作者：{authors_text.strip()[:400]}")
    src_parts.append(f"摘要：\n{(abstract or '').strip()[:2000]}")
    if pdf_text.strip():
        src_parts.append(f"PDF 首页文本（含单位 / 平台 / 实验信息）：\n{pdf_text.strip()[:3500]}")
    source = "\n\n".join(src_parts)

    # ① writer
    draft = _coerce(_deepseek_json(SYSTEM_PROMPT, source, key, model, timeout), model)
    if not review:
        return draft

    # ② reviewer：对照原文核对草稿，删掉没依据的
    draft_json = json.dumps({
        "institutions": draft.institutions,
        "method_tags": draft.method_tags,
        "platform": draft.platform,
        "sim_stack": draft.sim_stack,
        "real_robot": draft.real_robot,
    }, ensure_ascii=False)
    review_user = f"【原始文本】\n{source}\n\n【字段草稿】\n{draft_json}"
    try:
        reviewed = _coerce(_deepseek_json(REVIEW_PROMPT, review_user, key, model, timeout), model)
    except Exception as e:
        # reviewer 失败：不能直接信任未核验的 writer 草稿。保守降级——清空高风险
        # 事实字段（机构/平台/仿真栈，最容易被瞎猜），只留较低风险的 method_tags /
        # real_robot；并标记 review_ok=False，下次 build 重试核验。
        log.warning("enrich review failed (%s); 保守降级 + 标记重试", e)
        draft.institutions = []
        draft.platform = []
        draft.sim_stack = []
        draft.review_ok = False
        return draft
    return reviewed


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
    # 抽取 schema 版本：2 = 读 PDF 首页 + 默认弃权 + reviewer 复核（去掉
    # method_family / training_summary）。低于此版本的缓存条目需要重抽。
    SCHEMA = 2

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

    # PDF 文本下载失败时最多重试几次再放弃（防死链 PDF 每轮空耗 backfill 预算）。
    MAX_PDF_RETRIES = 3

    def needs_reenrich(self, pid: str, has_pdf_url: bool = False) -> bool:
        """是否需要(重新)抽取。
        - 没缓存 / schema 低于当前 → 需要。
        - schema 当前、但「有 pdf_url 却没读到 PDF 证据(pdf_ok=False)」或「reviewer
          没核验过(review_ok=False)」，且重试次数未超上限 → 需要（给上次的瞬时失败
          再试一次机会）。试满 MAX_PDF_RETRIES 次后放弃，不再空耗 backfill 预算。"""
        e = self.entries.get(pid)
        if not e:
            return True
        if int(e.get("schema", 0)) < self.SCHEMA:
            return True
        if int(e.get("tries", 0)) < self.MAX_PDF_RETRIES:
            if has_pdf_url and not e.get("pdf_ok"):
                return True
            if not e.get("review_ok", True):
                return True
        return False

    def put(self, pid: str, j: Enrichment, pdf_ok: bool = True, review_ok: bool = True) -> None:
        prev_tries = int((self.entries.get(pid) or {}).get("tries", 0))
        d = asdict(j)
        d.pop("review_ok", None)  # 用入参的 review_ok 为准（dataclass 上的只是传递）
        d["ts"] = int(time.time())
        d["schema"] = self.SCHEMA
        d["pdf_ok"] = bool(pdf_ok)
        d["review_ok"] = bool(review_ok)
        d["tries"] = prev_tries + 1
        self.entries[pid] = d

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
