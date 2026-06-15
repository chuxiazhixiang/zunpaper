"""DeepSeek-V4-Flash 论文相关性 & 科研价值门禁。

为什么要这玩意：
    频道关键词命中只能保证「字面相关」，命中 humanoid / VLA / 具身 等词
    并不代表「对我科研有帮助」。这里在 paper 上站之前，用一个便宜的 LLM
    判断一下：
      (1) 是否属于站长真正关心的方向？
      (2) 是否有科研价值（而不是行业八卦 / 融资新闻 / 演示视频）？
    只有 (1) ∩ (2) 都过的 paper 才上首页。

为什么用 V4-Flash 而不是 V4-Pro：
    判定本身是简单分类任务，不需要 reasoning。V4-Flash 价格只有 Pro 的
    1/3（$0.14/M 输入 vs $0.435/M），延迟也低很多。

用量估算：
    每篇文章: ~400 token 输入 + ~100 token 输出
    单次成本 ≈ 400×0.14/1M + 100×0.28/1M ≈ $0.000084 ≈ ¥0.0006
    每天新文章上限按 100 算 → 单日 ≤ ¥0.06，单月 ≤ ¥1.8

环境变量：
    DEEPSEEK_API_KEY        必填
    REDPAPER_JUDGE_MODEL    可选，默认 deepseek-v4-flash
    REDPAPER_JUDGE_DISABLE  设为 "1" 时跳过 judge（按一律 relevant 处理）

缓存：
    站点根 data/judge_cache.json （id → judgment），跨次 build 复用，
    避免重复付费。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("REDPAPER_JUDGE_MODEL", "deepseek-v4-flash")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"

JUDGE_TIMEOUT = 60

# judge prompt 版本号：改了 SYSTEM_PROMPT 的判定标准（频道定义 / 黑白名单 /
# VLA 归类等）就 +1。
#
# ⚠️ 本字段只「记录」每条判定是用哪版 prompt 做的，**不自动失效**。原因：judge
# 决定论文的去留（relevant=false 直接砍），若按版本自动全站重判，会突然大批
# 增删论文、且烧钱/可能超时，风险远高于 enrich（enrich 只改展示标签）。
# 想按新 prompt 重判时，手动跑 `JudgeCache(path).evict_stale()` 清掉旧版本条目
# （或只清近 N 天），下次 build 自然重判。详见 AGENTS.md「缓存与版本约定」。
PROMPT_VERSION = 1

SYSTEM_PROMPT = (
    "你是一位严格的具身机器人方向研究员，要从「研究相关性 + 科研价值」两个角度给文章打分。\n"
    "输入是一篇文章的标题 + 摘要 / 描述。\n"
    "用户关心的方向（6 大频道，一篇可属多个）：\n"
    "  - loco-manip-wbc：Loco-Manipulation 与 Whole-Body Control。\n"
    "      人形机器人全身控制（WBC）、移动操作（loco-manipulation）、人形动作模仿（mimic）、\n"
    "      动作重定向（retargeting）、人形机器人 + 家用 / 仓储 / 户外作业。\n"
    "      ★ VLA 的判别：如果是「whole-body VLA / 人形 VLA / humanoid foundation model」\n"
    "         （平台是 humanoid / G1 / H1 / H2 / Atlas / Optimus 等全身机器人，且涉及全身动作\n"
    "         或腿+臂协同），归这里。例如 WholeBodyVLA、Helix 全身、HumanoidVLA、Figure-02 全身。\n"
    "  - manipulation：Manipulation。\n"
    "      灵巧手、抓取、双臂操作、桌面 / 平台 / arm-only 任务、扩散策略、ACT、imitation learning。\n"
    "      ★ VLA 的判别：如果是「arm-only VLA / 操作 VLA」（平台是 Franka / UR / xArm /\n"
    "         WidowX / 灵巧手 / 双臂 / 桌面 / mobile-manipulator 但限在 manipulation 范畴），\n"
    "         归这里。例如 OpenVLA、π0 / π0.5 / π0.6、Octo、RDT、H-RDT、Helix（桌面版）、\n"
    "         EgoVLA、Being-H0、ACT、Diffusion Policy。\n"
    "      ★ 一般原则：通用 VLA / generalist policy / robot foundation model 默认走 manipulation，\n"
    "         除非作者明确强调是 humanoid 全身（题目 / 摘要里有 whole-body / humanoid 字样）。\n"
    "  - teleop：Teleoperation。VR / 主从 / 力反馈 / 沉浸式遥操作；human-in-the-loop 控制\n"
    "  - locomotion：Locomotion。双足 / 四足运动、parkour、gait、terrain、跳跃 / 奔跑\n"
    "  - sim2real：Sim-to-Real。仿真到真机迁移、域随机化、residual / 域适应、Isaac Sim/Lab\n"
    "  - world-model：World Model（用户特别关心，宁可放进来不要错砍）。\n"
    "      包括 JEPA / V-JEPA / I-JEPA / LeWorldModel、NVIDIA Cosmos / Cosmos-Reason、\n"
    "      Google Genie / Genie 2 / Genie 3、Dreamer 系列、视频世界模型 (video world model)、\n"
    "      4D / Gaussian / 物理世界模型、世界基础模型 (world foundation model)、\n"
    "      latent imagination policy、neural simulator、联合嵌入预测架构、\n"
    "      用作机器人/具身规划的可学习模拟器，以及具身领域的 world model 综述。\n"
    "      ★ 自动驾驶语境只要核心贡献是「通用 World Model 方法」（如 Waymo Genie 3、\n"
    "        小鹏 V-JEPA 端到端、华为 ADS 强化世界模型路线、蚂蚁灵波 LingBot-World、\n"
    "        Cosmos / GAIA-1 等）→ 一律 relevant=true，归 world-model 频道；\n"
    "        只有「自动驾驶端到端控制 / 单纯感知 / 量产新闻」没有提到 world model 才砍。\n"
    "      ★ 手机端 / 端上模型只要主题是 world model 也接受。\n"
    "**不要**接受的：\n"
    "  - 纯医疗 / 手术 / 康复机器人\n"
    "  - 工业流水线 SCARA / 机械臂自动化产线，缺乏算法新意\n"
    "  - 公司融资 / 招聘 / 行业沙龙活动 / 课程广告 / 会议宣传\n"
    "  - 自动驾驶车（仅排除「端到端驾驶控制 / 感知 / 量产宣传」；World Model / JEPA / 通用模拟器\n"
    "    类工作即便用在自驾场景也接受 —— 归 world-model 频道）\n"
    "  - 单纯硬件评测，缺乏算法或 policy 创新\n"
    "  - LLM-only / Agent / Computer-Use 等不涉及物理世界 / 不涉及 world model 的工作\n"
    "  - AIGC 漫剧、文生图、文生视频、合成人脸等娱乐方向（不含视频世界模型）\n"
    "**接受**的：\n"
    "  - 真机器人上跑的 RL / IL / imitation / VLA / VLN / world model\n"
    "  - 仿真器、数据集、benchmark（针对上述方向）\n"
    "  - 综述、教程（针对上述方向 —— 含 world model 综述）\n"
    "  - 知名实验室 / 公司 (Boston Dynamics, Figure, 1X, 宇树, 智元, 银河通用,\n"
    "    Physical Intelligence, Skild AI, Generalist, BAIR, Stanford SAIL,\n"
    "    NVIDIA GEAR / Cosmos, Google DeepMind robotics / Genie, Meta FAIR / V-JEPA,\n"
    "    Waymo, World Labs, 蚂蚁灵波, 小鹏, 华为车 BU, MIT CSAIL...) 发布的论文 / 演示\n"
    "  - 行业洞察类深度访谈，前提是受访者是上述方向的一线研究者 / 创始人\n"
    "严格按 JSON 输出，schema：\n"
    "{\n"
    '  "relevant": true/false,                  // 是否值得上 redpaper\n'
    '  "research_value": "high"/"medium"/"low", // 对一线科研者的参考价值\n'
    '  "primary_channel": "loco-manip-wbc|manipulation|teleop|locomotion|sim2real|world-model|none",\n'
    '  "reason": "20-40 字中文简评，说明为什么留 / 砍"\n'
    "}\n"
    "判定原则：\n"
    "  - 拿不准 → relevant=false 安全；宁缺勿滥。**但只要明确出现 world model / JEPA /\n"
    "    Genie / Cosmos / Dreamer 等关键词，就默认 relevant=true 归 world-model。**\n"
    "  - 标题是「Episode XX」「Video Friday」「Robot Talk」等连载娱乐栏目 → false。\n"
    "  - 仅有融资金额 / 估值 / 团队招聘信息 → false。\n"
    "  - 学术综述 / 数据集 / benchmark / 仿真器（在用户方向） → relevant=true, research_value=high。\n"
    "  - 单纯硬件展示 / 演示视频，无新算法 → low；relevant=true 仅当来自顶尖实验室且揭示新能力。\n"
)


@dataclass
class Judgment:
    relevant: bool = False
    research_value: str = "low"     # high / medium / low
    primary_channel: str = "none"
    reason: str = ""
    model: str = ""                  # 用了哪个模型
    raw: str = ""                    # debug 用，可选


# ---------- Public API ----------------------------------------------------

class JudgeUnavailable(RuntimeError):
    """Raised when judge is intentionally disabled (no API key / dryrun)."""


def judge_paper(title: str, abstract: str, *, model: str = DEFAULT_MODEL,
                api_key: str | None = None, timeout: float = JUDGE_TIMEOUT) -> Judgment:
    """同步调用 DeepSeek，对一篇论文做相关性判定。

    若环境变量 REDPAPER_JUDGE_DISABLE=1 直接 raise JudgeUnavailable。
    若没有 DEEPSEEK_API_KEY 也直接 raise JudgeUnavailable。
    """
    if os.environ.get("REDPAPER_JUDGE_DISABLE") == "1":
        raise JudgeUnavailable("REDPAPER_JUDGE_DISABLE=1")
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise JudgeUnavailable("DEEPSEEK_API_KEY not set")

    user_msg = (
        f"标题：{title.strip()}\n\n"
        f"摘要 / 描述：\n{(abstract or '').strip()[:3000]}\n"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        # V4-Flash 默认开 reasoning（completion 里 reasoning_tokens 会吃掉很多
        # 配额），导致剩下的 token 装不下 JSON 直接被截断 → 解析失败 →
        # 我们误判为 relevant=False。判定本身是简单分类，不需要 CoT，关掉。
        "thinking": {"type": "disabled"},
        # 给 200 token 是为了「JSON 主体 + 中文 reason 30-40 字」留余地。
        # 经验上一次完整响应 80-120 token，留 400 安全。
        "max_tokens": 400,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    r = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    data = _parse_response(raw)
    j = Judgment(
        relevant=bool(data.get("relevant", False)),
        research_value=str(data.get("research_value", "low")).lower(),
        primary_channel=str(data.get("primary_channel", "none")).lower(),
        reason=str(data.get("reason", "")).strip()[:200],
        model=model,
        raw="",  # 不存 raw，索引文件已经够大了
    )
    return j


REPO_SYSTEM_PROMPT = (
    "你是一位严格的具身 / 人形机器人方向研究员，要判断一个 GitHub 开源仓库\n"
    "是否值得放进「优质开源项目」栏目。输入是仓库的 名字 + 描述 + README 摘要 + topics。\n"
    "\n"
    "**接受**（relevant=true）—— 必须是「能落地的算法 / 模型 / 系统 / 框架 / 数据集 / benchmark」，\n"
    "且属于下列任一方向：\n"
    "  - loco-manip-wbc：人形全身控制 / 移动操作 / 全身 VLA / 人形动作模仿 / retargeting\n"
    "  - manipulation：灵巧手 / 抓取 / 双臂 / 桌面操作 / 操作 VLA（OpenVLA/π0/Octo/RDT/ACT/Diffusion Policy）\n"
    "  - teleop：遥操作 / VR / 力反馈 / 主从\n"
    "  - locomotion：双足 / 四足 / parkour / 地形运动\n"
    "  - sim2real：仿真到真机 / 域随机化 / 机器人仿真器（Isaac/MuJoCo/Genesis/ManiSkill）\n"
    "  - world-model：世界模型 / JEPA / Cosmos / Genie / Dreamer / 视频或物理世界模型\n"
    "典型正例：openvla、openpi(π0)、lerobot、IsaacLab、Genesis、diffusion_policy、HumanPlus、\n"
    "ASAP、human2humanoid、expressive-humanoid、AMO、RDT、GR00T、umi-on-legs、robosuite。\n"
    "\n"
    "**砍掉**（relevant=false）—— 这些即使 star 很高也不要：\n"
    "  - 课程 / 教程 / 学习路线 / awesome-list / 论文清单 / 面经（如 every-embodied、awesome-xxx）\n"
    "  - 别人的 fork / 非官方复现 / 架构草稿（lucidrains 式 re-implementation，跑不出系统）\n"
    "  - 蹭热门项目名的无关仓（如把 π0 名字用在游戏外挂 / 加密货币 / 网页模板）\n"
    "  - 个人镜像 / 作业 / 玩具 demo / 空壳仓（README 几乎没内容）\n"
    "  - 纯前端 / 网站 / 数据可视化 / 与物理机器人算法无关的通用 ML / LLM Agent\n"
    "  - 医疗 / 手术 / 工业产线自动化，缺乏机器人学习算法新意\n"
    "\n"
    "严格按 JSON 输出，schema：\n"
    "{\n"
    '  "relevant": true/false,                  // 是否值得进开源栏目\n'
    '  "research_value": "high"/"medium"/"low", // 对一线科研 / 工程的参考价值\n'
    '  "primary_channel": "loco-manip-wbc|manipulation|teleop|locomotion|sim2real|world-model|none",\n'
    '  "reason": "20-40 字中文简评，说明留 / 砍理由"\n'
    "}\n"
    "判定原则：\n"
    "  - 是「课程 / awesome / 复现 / 蹭名 / 空壳」→ 一律 false，无论多少 star。\n"
    "  - 官方算法 / 模型 / 框架 / 仿真器 / 数据集（在用户方向）→ true，research_value 视影响力。\n"
    "  - 拿不准是不是官方原作、或方向是否吻合 → false，宁缺勿滥。\n"
)


def judge_repo(name: str, description: str, readme: str = "", topics: str = "",
               *, model: str = DEFAULT_MODEL, api_key: str | None = None,
               timeout: float = JUDGE_TIMEOUT) -> Judgment:
    """对一个 GitHub 仓库做「是否进开源栏目」判定。复用 Judgment / 解析逻辑。"""
    if os.environ.get("REDPAPER_JUDGE_DISABLE") == "1":
        raise JudgeUnavailable("REDPAPER_JUDGE_DISABLE=1")
    key = api_key or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise JudgeUnavailable("DEEPSEEK_API_KEY not set")

    user_msg = (
        f"仓库：{name.strip()}\n"
        f"topics：{(topics or '').strip()}\n\n"
        f"描述：{(description or '').strip()[:500]}\n\n"
        f"README 摘要：\n{(readme or '').strip()[:2500]}\n"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REPO_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "thinking": {"type": "disabled"},
        "max_tokens": 400,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = requests.post(DEEPSEEK_URL, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    data = _parse_response(raw)
    return Judgment(
        relevant=bool(data.get("relevant", False)),
        research_value=str(data.get("research_value", "low")).lower(),
        primary_channel=str(data.get("primary_channel", "none")).lower(),
        reason=str(data.get("reason", "")).strip()[:200],
        model=model,
    )


def _parse_response(raw: str) -> dict:
    """LLM 偶尔会在 JSON 周围加 ```json fence；剥掉。"""
    s = raw.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except Exception:
        # 兜底：找到第一段 {...}
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


# ---------- Cache ---------------------------------------------------------

class JudgeCache:
    """跨次 build 复用判定结果，避免重复付费。

    存盘路径：data/judge_cache.json（仓库根，不在 site/ 里所以不会被
    publish 暴露）。Schema：
      {
        "version": 1,
        "model": "deepseek-v4-flash",
        "entries": { "<paper_id>": { "relevant":bool, ... , "ts":int } }
      }
    """

    VERSION = 1

    def __init__(self, path: Path):
        self.path = path
        self._data = {"version": self.VERSION, "model": DEFAULT_MODEL, "entries": {}}
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                log.warning("judge cache load failed (%s); starting fresh", e)

    @property
    def entries(self) -> dict[str, dict]:
        return self._data.setdefault("entries", {})

    def get(self, pid: str) -> Judgment | None:
        e = self.entries.get(pid)
        if not e:
            return None
        # 老条目可能 schema 不全 / 不同模型；做个最低限度校验
        return Judgment(
            relevant=bool(e.get("relevant", False)),
            research_value=e.get("research_value", "low"),
            primary_channel=e.get("primary_channel", "none"),
            reason=e.get("reason", ""),
            model=e.get("model", ""),
        )

    def put(self, pid: str, j: Judgment) -> None:
        d = asdict(j)
        d["ts"] = int(time.time())
        # 只记录用哪版 prompt 判的；不自动失效（见 PROMPT_VERSION 注释）。
        d["prompt_version"] = PROMPT_VERSION
        self.entries[pid] = d

    def evict_stale(self, current_version: int = PROMPT_VERSION,
                    newer_than_ts: int | None = None) -> int:
        """手动重判用：删掉 prompt_version 不等于 current_version 的条目，下次
        build 会重新判定。可选 newer_than_ts：只清这个时间戳之后入库的（即"只
        重判近期"），避免一次性全站重判带来的成本/论文增删波动。返回删除条数。
        注意：本方法不会在常规 build 流程里被自动调用。"""
        stale = []
        for pid, e in list(self.entries.items()):
            if int(e.get("prompt_version", 0)) == current_version:
                continue
            if newer_than_ts is not None and int(e.get("ts", 0)) < newer_than_ts:
                continue
            stale.append(pid)
        for pid in stale:
            self.entries.pop(pid, None)
        return len(stale)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
