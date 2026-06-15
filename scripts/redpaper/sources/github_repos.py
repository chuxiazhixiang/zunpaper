"""GitHub 开源项目源 —— 把高质量的具身/人形机器人算法仓库做成卡片。

发现策略（见仓库根 调研结论）：
  - 纯 star 不可靠：课程 / awesome-list 仓能上 2k+ star，而真正有影响力的论文仓
    （AMO / ExBody / HumanPlus）只有 350–850。所以这里只负责「召回 + 初筛」，
    把候选交给 judge.judge_repo 让 LLM 砍掉课程 / 复现 / 蹭名 / 跑题。
  - 召回：在一组关键词上跑 GitHub search，stars >= min_stars，fork:false，
    按 star 倒序，跨关键词去重。
  - 经典老仓（diffusion_policy 这种很久没更新但仍是金标准）不按更新时间硬砍。

鉴权：优先用环境变量 GITHUB_TOKEN（CI 自带 / 本地可传 `gh auth token`）。
没有 token 也能跑，只是 search API 限额低（10 req/min），容易被限流。
"""
from __future__ import annotations

import logging
import os
import re
import time
from urllib.parse import quote

import requests

from ..models import Author, Paper

log = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
TIMEOUT = 30

# 召回关键词集（覆盖站点 6 个方向）。每个查询取 star 最高的若干个，去重后汇总。
DEFAULT_QUERIES = [
    "humanoid robot",
    "vision language action robot",
    "robot manipulation policy",
    "legged locomotion reinforcement learning",
    "whole body control humanoid",
    "embodied AI agent",
    "robot learning imitation",
    "world model robot",
    "sim2real robot",
    "dexterous manipulation",
    "teleoperation robot",
    "diffusion policy robot",
]


def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "redpaper/0.1 (+https://github.com/Nangongyeee/redpaper)",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _search(query: str, min_stars: int, per_page: int = 15) -> list[dict]:
    q = f"{query} stars:>={min_stars} fork:false"
    url = f"{GITHUB_API}/search/repositories?q={quote(q)}&sort=stars&order=desc&per_page={per_page}"
    try:
        r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        if r.status_code == 403 and "rate limit" in (r.text or "").lower():
            log.warning("github search rate-limited on «%s»; backing off 20s", query)
            time.sleep(20)
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("items", []) or []
    except Exception as e:
        log.warning("github search «%s» failed: %s", query, e)
        return []


def _fetch_readme_excerpt(full_name: str, max_chars: int = 2000) -> str:
    """拉 README 前 max_chars 字符给 LLM 判定用（去掉 badge / HTML 噪音）。"""
    url = f"{GITHUB_API}/repos/{full_name}/readme"
    try:
        r = requests.get(url, headers={**_headers(), "Accept": "application/vnd.github.raw"},
                         timeout=TIMEOUT)
        if r.status_code != 200:
            return ""
        text = r.text or ""
    except Exception:
        return ""
    # 去掉 markdown 图片 / badge 行 / HTML 标签，保留正文语义
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)       # ![img](...)
    text = re.sub(r"<[^>]+>", " ", text)                      # html tags
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)   # code fences
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def fetch_candidate_repos(
    min_stars: int = 300,
    max_repos: int = 120,
    queries: list[str] | None = None,
    with_readme: bool = True,
) -> list[dict]:
    """跨关键词召回候选仓，去重，按 star 倒序，截断到 max_repos。

    返回 dict 列表，字段：full_name / owner / repo / stars / language /
    description / topics / html_url / pushed_at / created_at / readme。
    """
    queries = queries or DEFAULT_QUERIES
    by_name: dict[str, dict] = {}
    for q in queries:
        for it in _search(q, min_stars):
            full = it.get("full_name")
            if not full or full in by_name:
                continue
            owner, _, repo = full.partition("/")
            by_name[full] = {
                "full_name": full,
                "owner": owner,
                "repo": repo,
                "stars": it.get("stargazers_count", 0),
                "language": it.get("language") or "",
                "description": (it.get("description") or "").strip(),
                "topics": it.get("topics") or [],
                "html_url": it.get("html_url") or f"https://github.com/{full}",
                "pushed_at": (it.get("pushed_at") or "")[:10],
                "created_at": (it.get("created_at") or "")[:10],
                "archived": bool(it.get("archived")),
            }
        time.sleep(1.5)  # search API: 30/min (token) — 留余量

    repos = sorted(by_name.values(), key=lambda d: d["stars"], reverse=True)[:max_repos]
    if with_readme:
        for d in repos:
            d["readme"] = _fetch_readme_excerpt(d["full_name"])
            time.sleep(0.4)
    log.info("github: %d candidate repos (min_stars=%d, queries=%d)",
             len(repos), min_stars, len(queries))
    return repos


def _slug(full_name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", full_name.lower()).strip("-")
    return f"github-{s}"


def repo_to_paper(d: dict) -> Paper:
    """把候选仓 dict 包成 Paper（source="github"）。

    - published 故意留空：开源仓不进每日归档 / 排行（那是给论文的），只在
      「开源项目」tab 里按 star 排。created/pushed 时间存进 github 元数据展示。
    - abstract = description + README 摘要，喂给 judge / translate。
    """
    abstract = d.get("description", "")
    readme = d.get("readme", "")
    if readme:
        abstract = (abstract + "\n\n" + readme).strip()
    return Paper(
        id=_slug(d["full_name"]),
        source="github",
        title=d["full_name"],
        abstract=abstract,
        authors=[Author(name=d.get("owner", ""))],
        published="",                       # 不进归档/排行
        updated=d.get("pushed_at", ""),
        abs_url=d.get("html_url", ""),
        # channels 由 _process_github_repos 根据 judge 判定的方向填充
        # （loco-manip-wbc / manipulation / ...），让二级方向标签能过滤开源项目。
        channels=[],
        source_tags=["github"],
        github={
            "owner": d.get("owner", ""),
            "repo": d.get("repo", ""),
            "stars": d.get("stars", 0),
            "language": d.get("language", ""),
            "pushed_at": d.get("pushed_at", ""),
            "created_at": d.get("created_at", ""),
            "topics": d.get("topics", []),
            "archived": d.get("archived", False),
        },
    )
