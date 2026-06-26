"""从 arXiv comment / 文本里识别会议 / 期刊（venue）。

arXiv 论文作者常在 `comment` 字段写「Accepted to CoRL 2025」「RSS 2026」之类，
这是免费、就在 arXiv 元数据里的信号——比去爬各会议官网省事得多。这里用一个
带年份约束的规则把它解析成规范标签（如 "CoRL 2025"）。要求**必须出现 4 位年份**
才认（否则像 "we compare with CVPR baselines" 会误判），降低误报。
"""
from __future__ import annotations

import re

# (正则, 规范名)。顺序靠前的优先。覆盖站长点名的具身/机器人/ML 主流会议期刊。
_VENUE_PATTERNS: list[tuple[str, str]] = [
    (r"Science\s+Robotics", "Science Robotics"),
    (r"\bIJRR\b|International Journal of Robotics Research", "IJRR"),
    (r"\bT-?RO\b|Transactions on Robotics", "T-RO"),
    (r"\bT-?MECH\b|Transactions on Mechatronics", "T-MECH"),
    (r"\bRA-?L\b|Robotics and Automation Letters", "RA-L"),
    (r"\bTPAMI\b|Pattern Analysis and Machine Intelligence", "TPAMI"),
    (r"\bIROS\b", "IROS"),
    (r"\bICRA\b", "ICRA"),
    (r"\bCoRL\b|Conference on Robot Learning", "CoRL"),
    (r"\bRSS\b|Robotics:?\s*Science and Systems", "RSS"),
    (r"\bICLR\b", "ICLR"),
    (r"\bNeurIPS\b|\bNIPS\b", "NeurIPS"),
    (r"\bICML\b", "ICML"),
    (r"\bCVPR\b", "CVPR"),
    (r"\bICCV\b", "ICCV"),
    (r"\bECCV\b", "ECCV"),
    (r"\bAAAI\b", "AAAI"),
    (r"\bCDC\b|Conference on Decision and Control", "CDC"),
    (r"\bTAC\b|Transactions on Automatic Control", "TAC"),
    (r"\bL4DC\b|Learning for Dynamics", "L4DC"),
    (r"\bHumanoids\b", "Humanoids"),
]

_YEAR_RE = re.compile(r"\b(20\d{2})\b")
# 仅当出现这些"录用语境"或紧挨年份时才认，避免把"对比了 CVPR 方法"误判。
_ACCEPT_HINT = re.compile(
    r"accept|to appear|camera[- ]?ready|proceedings|published in|presented at|"
    r"录用|收录|接收|发表",
    re.IGNORECASE,
)


def parse_venue(text: str) -> str:
    """从一段文本（通常是 arXiv comment）解析出规范 venue 标签，如 "CoRL 2025"。
    解析不出返回 ""。要求文本里有 4 位年份；venue 名远离录用语境时更保守
    （要求 venue 与某个年份相邻，避免"对比了 CVPR 方法"之类顺带提及）。"""
    if not text:
        return ""
    years = [m.start() for m in _YEAR_RE.finditer(text)]
    if not years:
        return ""  # 没有年份不认，降低误报
    year = _YEAR_RE.search(text).group(1)
    has_hint = bool(_ACCEPT_HINT.search(text))
    for pat, label in _VENUE_PATTERNS:
        starts = [m.start() for m in re.finditer(pat, text, re.IGNORECASE)]
        if not starts:
            continue
        # 取 venue 出现位置与任一年份的最近距离；有录用语境则直接认。
        nearest = min(abs(s - y) for s in starts for y in years)
        if has_hint or nearest <= 30:
            return f"{label} {year}"
    return ""
