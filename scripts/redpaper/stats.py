"""站点数据可视化的预聚合 → site/data/stats.json。

每天 build 末尾跑一次（纯内存，对 Paper 列表做统计，很便宜），前端「📊 数据」
页直接读这个 JSON 画图，保证每天更新且加载快（不在浏览器里现算上千篇）。

为了能在不跑完整 build 的情况下本地自测，`compute_stats` 用 `_g` 同时兼容
Paper 对象（属性访问）和 feed-entry dict（index.json 里的）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from collections import Counter

from . import config as cfg

log = logging.getLogger(__name__)

# 热点关键词追踪：label → 命中词（在 标题+摘要+tldr+method_tags 里做小写子串匹配）
_HOT_KEYWORDS = [
    ("World Model", ["world model", "世界模型", "v-jepa", "jepa", "dreamer", "cosmos", "genie"]),
    ("VLA", ["vision-language-action", "vision language action", "openvla", "vla"]),
    ("人形/全身", ["humanoid", "人形", "whole-body", "whole body", "loco-manipulation"]),
    ("Diffusion", ["diffusion policy", "diffusion model", "扩散策略", "扩散模型"]),
    ("Sim2Real", ["sim-to-real", "sim2real", "sim to real", "仿真到现实", "域随机"]),
    ("强化学习", ["reinforcement learning", "强化学习"]),
    ("遥操作", ["teleoperation", "遥操作"]),
]

_N_MONTHS = 12


def _g(p, field, default=None):
    """兼容 Paper 对象（getattr）和 dict（get）。"""
    if isinstance(p, dict):
        v = p.get(field, default)
    else:
        v = getattr(p, field, default)
    return v if v is not None else default


def _month_list(n: int = _N_MONTHS) -> list[str]:
    today = dt.date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def _merge_ci(counter: Counter) -> Counter:
    """大小写不敏感合并（"Sim-to-real" / "sim-to-real" 算一个），display 取出现
    最多的那个原始写法。"""
    groups: dict[str, dict] = {}
    for raw, n in counter.items():
        key = raw.lower()
        g = groups.setdefault(key, {"total": 0, "variants": Counter()})
        g["total"] += n
        g["variants"][raw] += n
    out = Counter()
    for g in groups.values():
        display = g["variants"].most_common(1)[0][0]
        out[display] = g["total"]
    return out


def _ci_display_map(counter: Counter) -> dict[str, str]:
    """{小写 key → 展示名}，展示名取该 key 下出现最多的原始写法。用来把 per-channel
    的 tag 归一到与全局榜一致的展示名（method / platform 用）。"""
    groups: dict[str, Counter] = {}
    for raw, n in counter.items():
        groups.setdefault(raw.lower(), Counter())[raw] += n
    return {k: v.most_common(1)[0][0] for k, v in groups.items()}


def _src_group(s: str) -> str:
    s = (s or "").lower()
    if s in ("arxiv", "arxiv_discover", "manual_arxiv"):
        return "arXiv 论文"
    if s.startswith("video_"):
        return "厂商视频"
    if s == "github":
        return "开源项目"
    if s == "external_link":
        return "外部链接"
    if s in ("qbitai", "embodied_techdaily", "shenlan_embodied", "jiqizhixin", "manual_xhs"):
        return "公众号/社区"
    return "行业媒体"


def compute_stats(all_papers, channels) -> dict:
    months = _month_list()
    mset = set(months)
    ch_meta = [{"id": c.id, "name": c.name, "emoji": c.emoji} for c in channels]
    ch_ids = [c.id for c in channels]

    papers = [p for p in all_papers if _g(p, "source", "") != "github"]
    repos = [p for p in all_papers if _g(p, "source", "") == "github"]

    # ① 各分类论文数
    cat_count = {cid: 0 for cid in ch_ids}
    # ② 各分类随时间（月）
    cat_time = {cid: {mm: 0 for mm in months} for cid in ch_ids}
    for p in papers:
        chs = _g(p, "channels", []) or []
        mm = (_g(p, "published", "") or "")[:7]
        for c in chs:
            if c in cat_count:
                cat_count[c] += 1
                if mm in mset:
                    cat_time[c][mm] += 1

    # ③ 新增趋势：日（近 365 天，给日历热力图）+ 月总量
    cutoff = (dt.date.today() - dt.timedelta(days=365)).isoformat()
    daily = Counter()
    monthly_total = {mm: 0 for mm in months}
    for p in papers:
        d = (_g(p, "published", "") or "")[:10]
        if len(d) == 10:
            if d >= cutoff:
                daily[d] += 1
            if d[:7] in mset:
                monthly_total[d[:7]] += 1
    intake_daily = sorted([d, n] for d, n in daily.items())

    # ④ 机构活跃榜
    inst = Counter()
    for p in papers:
        for i in (_g(p, "institutions", []) or []):
            if i:
                inst[i] += 1
    institutions = [{"name": k, "count": v} for k, v in inst.most_common(15)]

    # ⑤ 技术趋势：method_tags 排行 + top6 随时间
    mt = _merge_ci(Counter(
        t for p in papers for t in (_g(p, "method_tags", []) or []) if t
    ))
    method_top = [{"name": k, "count": v} for k, v in mt.most_common(12)]
    top6 = [k for k, _ in mt.most_common(6)]
    top6_lower = {k.lower(): k for k in top6}
    method_time = {k: {mm: 0 for mm in months} for k in top6}
    for p in papers:
        mm = (_g(p, "published", "") or "")[:7]
        if mm not in mset:
            continue
        seen = set()
        for t in (_g(p, "method_tags", []) or []):
            disp = top6_lower.get((t or "").lower())
            if disp and disp not in seen:
                method_time[disp][mm] += 1
                seen.add(disp)

    # ⑦ 机器人本体热度（platform）
    plat = _merge_ci(Counter(
        t for p in papers for t in (_g(p, "platform", []) or []) if t
    ))
    platform = [{"name": k, "count": v} for k, v in plat.most_common(12)]
    platform_disclosed = sum(1 for p in papers if (_g(p, "platform", []) or []))

    # ⑧ 来源构成（含 github）
    src = Counter(_src_group(_g(p, "source", "")) for p in all_papers)
    source = [{"name": k, "value": v} for k, v in src.most_common()]

    # ⑨ 热点关键词随时间
    hot = {label: {mm: 0 for mm in months} for label, _ in _HOT_KEYWORDS}
    for p in papers:
        mm = (_g(p, "published", "") or "")[:7]
        if mm not in mset:
            continue
        hay = " ".join([
            _g(p, "title", "") or "",
            _g(p, "abstract", "") or "",
            _g(p, "tldr_zh", "") or "",
            " ".join(_g(p, "method_tags", []) or []),
        ]).lower()
        for label, terms in _HOT_KEYWORDS:
            if any(t in hay for t in terms):
                hot[label][mm] += 1

    # ---- 按方向拆分（供数据看板「方向筛选」过滤下方图表）-----------------
    # 机构/方法/本体/热点这些榜原本是全站汇总、没按方向拆，所以前端筛选影响不到。
    # 这里给每个频道单独存一份分布，前端按勾选的方向求和重排即可。一篇论文若属
    # 多个频道，会在每个所属频道里各记一次（与 cat_count 的语义一致）。
    # 为控制体积：机构/方法/本体只保留全站出现 ≥2 次的名字（去掉一次性长尾）。
    method_disp = _ci_display_map(Counter(
        t for p in papers for t in (_g(p, "method_tags", []) or []) if t))
    plat_disp = _ci_display_map(Counter(
        t for p in papers for t in (_g(p, "platform", []) or []) if t))
    inst_keep = {k for k, v in inst.items() if v >= 2}
    method_keep = {k for k, v in mt.items() if v >= 2}
    plat_keep = {k for k, v in plat.items() if v >= 2}
    method_top_names = [m["name"] for m in method_top]
    method_top_set = set(method_top_names)

    inst_by_ch = {cid: Counter() for cid in ch_ids}
    method_by_ch = {cid: Counter() for cid in ch_ids}
    platform_by_ch = {cid: Counter() for cid in ch_ids}
    hot_by_ch = {cid: {label: {mm: 0 for mm in months} for label, _ in _HOT_KEYWORDS} for cid in ch_ids}
    method_time_by_ch = {cid: {k: {mm: 0 for mm in months} for k in method_top_names} for cid in ch_ids}

    for p in papers:
        chs = [c for c in (_g(p, "channels", []) or []) if c in inst_by_ch]
        if not chs:
            continue
        mm = (_g(p, "published", "") or "")[:7]
        for i in (_g(p, "institutions", []) or []):
            if i and i in inst_keep:
                for c in chs:
                    inst_by_ch[c][i] += 1
        seen_m = set()
        for t in (_g(p, "method_tags", []) or []):
            disp = method_disp.get((t or "").lower())
            if not disp:
                continue
            if disp in method_keep:
                for c in chs:
                    method_by_ch[c][disp] += 1
            if disp in method_top_set and mm in mset and disp not in seen_m:
                for c in chs:
                    method_time_by_ch[c][disp][mm] += 1
                seen_m.add(disp)
        for t in (_g(p, "platform", []) or []):
            disp = plat_disp.get((t or "").lower())
            if disp and disp in plat_keep:
                for c in chs:
                    platform_by_ch[c][disp] += 1
        if mm in mset:
            hay = " ".join([
                _g(p, "title", "") or "",
                _g(p, "abstract", "") or "",
                _g(p, "tldr_zh", "") or "",
                " ".join(_g(p, "method_tags", []) or []),
            ]).lower()
            for label, terms in _HOT_KEYWORDS:
                if any(t in hay for t in terms):
                    for c in chs:
                        hot_by_ch[c][label][mm] += 1

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "totals": {"papers": len(papers), "repos": len(repos), "all": len(all_papers)},
        "months": months,
        "channels": ch_meta,
        "cat_count": cat_count,
        "cat_time": cat_time,
        "intake_daily": intake_daily,
        "intake_monthly": monthly_total,
        "institutions": institutions,
        "method_top": method_top,
        "method_time": method_time,
        "platform": platform,
        "platform_disclosed": platform_disclosed,
        "source": source,
        "hot_keywords": hot,
        # 按方向拆分（前端方向筛选用；老前端忽略这些键即可）
        "inst_by_ch": {cid: dict(c) for cid, c in inst_by_ch.items()},
        "method_by_ch": {cid: dict(c) for cid, c in method_by_ch.items()},
        "platform_by_ch": {cid: dict(c) for cid, c in platform_by_ch.items()},
        "method_time_by_ch": method_time_by_ch,
        "hot_by_ch": hot_by_ch,
    }


def write_stats(all_papers, channels) -> None:
    try:
        data = compute_stats(all_papers, channels)
    except Exception as e:
        log.warning("stats compute failed: %s", e)
        return
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (cfg.DATA_DIR / "stats.json").open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    log.info("stats written: %d papers / %d repos", data["totals"]["papers"], data["totals"]["repos"])
