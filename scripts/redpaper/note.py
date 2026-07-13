"""Generate a structured paper note via DeepSeek during build.

The note is cached in the paper JSON (`paper.note`). Only papers without
a note get processed, so re-running build is safe and cheap.
"""
from __future__ import annotations

import json
import logging
import os

import requests

from .models import Paper

log = logging.getLogger(__name__)

# 标签体系（供 LLM 选择）
TAG_PROMPT = """论文类型（选1-3个）：
- 论文/方法 — 网络结构、模块、损失函数、问题建模
- 论文/数据 — 数据采集、标注、清洗
- 论文/训练 — 损失设计、优化器、训练技巧
- 论文/系统 — 系统集成、工程优化、部署方案
- 论文/理论 — 数学证明、性质分析
- 论文/综述 — 分类体系、时间线
- 论文/应用 — 场景适配、工程改进

领域（选1-2个）：
- Domain/CV — 计算机视觉
- Domain/Robotics — 机器人学
- Domain/NLP — 自然语言处理
- Domain/Multimodal — 多模态

任务（选1-3个）：
- Task/VideoGen / Task/ImageGen / Task/CameraControl
- Task/I2V / Task/V2V / Task/LanguageModeling
- Task/EmbodiedAI / Task/RoboticManipulation
- Task/CrossEmbodimentTransfer / Task/Teleoperation
- Task/HardwareCoDesign / Task/PointCloud

状态：状态/待读"""

SYSTEM_PROMPT = f"""你是一位论文笔记助手。根据论文信息，生成一篇结构化的中文笔记。

## 输出格式
必须输出 JSON，不要有 markdown 代码块包裹：

{{"tags": ["论文/方法", "Domain/CV", ...], "motivation": "解决了什么不足（1-2 句）", "method": "方法一句话总结", "results": "最重要的 1-3 条结论，要有具体数字", "insights": "局限或启发（可写'与课题不相关'）"}}

## 标签规则

{TAG_PROMPT}

## 写作要求
- 动机、方法、结果、启发各一段简洁中文
- 结果要有具体数字
- 如果论文与机器人 / 具身智能不直接相关，诚实写「与课题不相关」
- 领域和任务尽量从上述标签中选择，找不到再创建新标签"""


def _build_prompt(paper: Paper) -> str:
    authors = ", ".join(a.name for a in (paper.authors or []))
    return (
        f"标题: {paper.title}\n"
        f"摘要: {paper.abstract}\n"
        f"作者: {authors}\n"
        f"分类: {', '.join(paper.categories or [])}\n"
        f"请输出 JSON，包含 tags / motivation / method / results / insights 五个字段。"
    )


def generate_note(paper: Paper) -> str:
    """Call DeepSeek to generate a note, return markdown string with YAML front matter."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        log.warning("DEEPSEEK_API_KEY not set, skipping note for %s", paper.id)
        return ""

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "temperature": 0.3,
                "max_tokens": 1536,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(paper)},
                ],
            },
            timeout=60,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]

        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # DeepSeek 偶尔返回引号未转义的非法 JSON，尝试修补
            import re as _re
            m = _re.search(r'\{.*"tags"\s*:\s*\[.*?"motivation"\s*:.*?"method"\s*:.*?"results"\s*:.*?"insights"\s*:.*?\}', text, _re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    log.warning("note for %s: JSON repair failed", paper.id)
                    return ""
            else:
                log.warning("note for %s: no valid JSON found in response", paper.id)
                return ""

        tags = obj.get("tags") or []
        motivation = (obj.get("motivation") or "").strip()
        method = (obj.get("method") or "").strip()
        results = (obj.get("results") or "").strip()
        insights = (obj.get("insights") or "").strip()

        if not any([motivation, method, results, insights]):
            log.warning("note for %s returned empty fields", paper.id)
            return ""

        # 组装 front matter
        tag_lines = "\n".join(f"  - {t}" for t in tags if t.startswith(("论文/", "Domain/", "Task/", "状态/")))
        published = (paper.published or "")[:4]
        front_matter = (
            "---\n"
            f"tags:\n{tag_lines}\n"
            f'Title: "{paper.title}"\n'
            f'Year: "{published}"\n'
            f"URL: {paper.abs_url or paper.pdf_url or ''}\n"
            "---\n"
        )

        # 组装正文
        body = ""
        if method:
            body += f">[!abstract] 一句话总结：{method}\n\n"
        body += "\n# 一、动机\n\n"
        body += f"> {motivation}\n\n" if motivation else ""
        body += "\n# 二、方法\n\n"
        body += f"> {method}\n\n" if method else ""
        body += "\n# 三、核心实验结果\n\n"
        body += f"> {results}\n\n" if results else ""
        body += "\n# 四、对我的启发\n\n"
        body += f"> {insights}\n" if insights else ""

        return front_matter + body

    except Exception as e:
        log.warning("note generation failed for %s: %s", paper.id, e)
        return ""
