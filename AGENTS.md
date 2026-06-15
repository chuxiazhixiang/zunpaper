# AGENTS.md — redpaper

面向 AI agent 的项目速读。读完这一篇就能上手，不用翻聊天记录。用户文档见 `README.md`。

## 这是什么

一个**零后端的静态站**：每天用 GitHub Actions 抓机器人/具身智能论文 + 中文公众号 + 厂商 demo 视频，过 LLM 把关 + 抽结构化标签 + 中文翻译 + 渲染 PDF 封面，生成 JSON/HTML 部署到 GitHub Pages，前端是「小红书 feed」风格。所有数据（论文 JSON、封面图、LLM 缓存）都 commit 进 git。

- 线上：[https://Nangongyeee.github.io/redpaper/](https://Nangongyeee.github.io/redpaper/)
- 仓库：`Nangongyeee/redpaper`，主分支 `main`
- 站点方向：人形/具身机器人（6 个频道，见下）
- 成本：LLM 一个月 ≈ ¥2（激进缓存 + 便宜的 deepseek-v4-flash）

## 技术栈

- **后端/管道**：纯 Python 3.11，依赖只有 5 个（`scripts/requirements.txt`：arxiv / pyyaml / requests / pymupdf / Pillow）。无框架、无数据库。
- **前端**：原生 HTML/CSS/JS（无构建步骤），`localStorage` 存收藏/深色模式。`site/` 直接就是 Pages 根。
- **LLM**：DeepSeek（judge / enrich / 月度综述，模型 `deepseek-v4-flash`）；Gemini（翻译首选 + 联网发现论文 grounded search）；OpenAI（翻译兜底）。
- **CI/CD**：`.github/workflows/daily.yml`（定时全量跑）+ `deploy.yml`（`site/`** push 时重新部署）。

## 管道总览（`scripts/redpaper/build.py::run()`）

执行顺序（每天 build 全量重跑，靠缓存省钱）：

1. `retag_and_prune` — 按当前 `channels.yaml` 重新给存量论文打频道 + 删除已不相关的 + 标题级去重
2. **抓取**汇入一个 `fresh: dict[id→Paper]`：
  - arXiv（`sources/arxiv_source.py`，近 2 天，不够回补 30 天 evergreen）
  - 公众号（`sources/cn_news.py`：量子位 qbitai / 具身智能之心 / 深蓝具身智能）
  - LLM 联网发现（`discover.py`，Gemini grounded search，P7）
  - 视频频道（`sources/video_channels.py`：YouTube + Bilibili 厂商 demo，P5）
  - 手动钉（`sources/manual_arxiv.py` ← `config/manual_arxiv.yaml`，绕过 judge）
3. `dedup_by_title` — 标题归一化二次去重（公众号同篇多 URL）
4. **judge**（`judge.py`）— DeepSeek 判 `{relevant, research_value, primary_channel, reason}`，`relevant=false` 砍掉；`manual_pin` 跳过
5. **enrich**（`enrich.py`）— DeepSeek 抽 7 字段（见 Paper 模型）
6. **demo 视频**（`videos.py`，P0）— 扫摘要/项目页找 YouTube/Bilibili/mp4
7. **translate + render**（`process_new_papers`）— 翻译标题/摘要/TL;DR + PyMuPDF 渲染封面
8. `write_feed` / `write_rss` / markdown digest
9. **月度综述**（`monthly_digest.py`，P6）— 只重算本月
10. `stamp_assets` — 给 HTML/JS 打 `?v=<git-sha>` cache-bust
11. Actions commit 数据 → push → Pages 部署

徽章 + 评分在 `EnrichmentContext.apply()`（build.py ~271）里对**每篇**论文（含存量）重算，所以改了 `labs.py` / `scoring.py` / `famous_labs.yaml` / `scoring.yaml` 后，**下次 build 会自动重刷存量论文**，不用手动改数据。

## 目录速查

```
config/                      # 所有可调项（改这里，不要硬编码）
  channels.yaml              # 6 频道 + 关键词（最重要）
  sources.yaml               # 数据源开关 + 各种阈值
  site.yaml                  # 站名/主色/翻译后端
  scoring.yaml               # 「为啥今天选了它」打分规则
  famous_labs.yaml           # ⭐ lab / 关键作者徽章规则
  manual_arxiv.yaml          # 站长钉论文
scripts/
  build.py                   # 入口（= python -m redpaper.build 的薄封装）
  api_push.py                # ★ git push TLS 失败时的兜底（走 gh API 推 main）
  audit_judge.py / import_awesome.py / migrate_channels.py / backfill_p1_p0.py
  run_monthly_digest.py / ingest_video_channels.py / add_paper.py / dev_run.py
  redpaper/
    build.py                 # 主编排（run() 在最底部）
    config.py                # YAML → dataclass（load_channels / load_sources）
    models.py                # Paper / Author 数据结构
    judge.py enrich.py discover.py translate.py monthly_digest.py  # LLM 环节
    videos.py                # demo 视频抽取
    render.py                # PDF → JPG
    scoring.py labs.py       # 打分 + 徽章（两者共用 famous_labs 规则）
    digest.py                # RSS / markdown
    sources/                 # arxiv / cn_news / video_channels / manual_* / hf_daily / semantic_scholar
site/                        # Pages 根（HTML + assets + data/ 生成物）
  *.html                     # index/post/archive/favorites/rankings/monthly/about
  assets/js/                 # 前端逻辑（feed.js/post.js/rankings.js/mascot.js…）
  data/                      # 管道产出（index.json / papers/{id}.json / daily/ / digest/ …）
data/                        # ★ LLM 缓存（judge_cache/enrich_cache/video_cache），CI commit 回仓库
```

## 跑起来

```bash
pip install -r scripts/requirements.txt
REDPAPER_LLM_BACKEND=dryrun python scripts/build.py        # 不调 LLM，验证管道
DEEPSEEK_API_KEY=sk-xxx python scripts/build.py            # 完整跑（judge/enrich/翻译/综述都 DeepSeek）
cd site && python -m http.server 8000                       # 本地预览
```

环境变量：`REDPAPER_LLM_BACKEND`（翻译后端 gemini|deepseek|openai|dryrun）；`DEEPSEEK_API_KEY`（judge/enrich/综述，必填）；`GEMINI_API_KEY`（翻译 + 联网发现，没有则 discover 整步跳过）；`REDPAPER_*_MODEL`（覆盖各环节模型）。

## 关键约定 / 缓存

- **多层缓存让重跑近乎免费**，同一篇只 LLM 一次：`data/judge_cache.json`（判定）、`data/enrich_cache.json`（标签）、`data/video_cache.json`（视频）、`site/data/papers/{id}.json`（翻译 + 渲染）。**这些缓存必须 commit 回仓库**（`daily.yml` 里 `git add data/*.json`），否则每天重判一遍浪费钱。要强制重判某篇 = 删它的 cache entry。
- **改频道/打分/徽章规则后无需手动改数据**：下次 build 对所有存量论文重算 `retag_and_prune` + `apply()`（badges + score）+ 重新过滤。
- **关键词宁多勿少**：`channels.yaml` 召回粗放，靠 judge 二审，漏召回比误召回更糟。
- **频道**：`loco-manip-wbc`（含全身/人形 VLA）、`manipulation`（含纯机械臂/桌面 VLA：OpenVLA/π0/Octo/RDT）、`teleop`、`locomotion`、`world-model`、`sim2real`。VLA 按形态分流：人形全身→loco-manip-wbc，机械臂→manipulation（规则在 `judge.py` SYSTEM_PROMPT + `channels.yaml`）。

## 踩过的坑（重要，别重蹈）

- **本机 `git push` / `git fetch` 走 git 协议会 TLS 报错/卡死**。要推代码用 `scripts/api_push.py`（走 gh Git Database API，硬编码推 `main`）；推 feature 分支得用 gh API 自己建 blob/tree/commit/ref。本地 `main` 经常停在旧 commit（没法 fetch），判断远端状态用 `gh api repos/Nangongyeee/redpaper/...`，别信本地 `git log`。
- **GitHub Actions schedule 会大幅延迟**（高峰期主 cron `0 3 * * `* 常拖几小时）。所以有 `0 6 * * *` 兜底。要立刻出数据：`gh workflow run daily.yml --ref main`。
- **arXiv 投递时刻**：周一到五美东 20:00 推当天公告（≈ 北京次日 08:00–09:00），**周末不发**。所以周末/周一站点常"没新论文"——不是 bug，是上游没东西。
- **作者重名误标**：常见名（如「Yue Wang」USC vs 浙大）只按名字子串匹配会错标。已引入**机构守卫** `require_affiliation`（`labs.py::author_rule_matches` + `famous_labs.yaml`）：作者名命中之外还要机构 haystack 命中对应学校。机构 haystack = per-author affiliation + `paper.institutions`(enrich 抽的) + 摘要（arXiv 的 per-author affiliation 基本为空，所以 `institutions` 是关键）。`labs.py` 和 `scoring.py` 共用这套逻辑，改一处即可。
- **discover 不能用 DeepSeek 兜底**：DeepSeek 没联网能力，纯记忆会编连号假 arxiv ID（2504.12345/12346…）。只用 Gemini 多模型 fallback（2.5-flash→2.0-flash→2.5-flash-lite），候选 ID 全部走 arxiv API 验真。
- **qbitai 日期**：archive 页要解析每条的 `<span class="time">`（绝对日期/昨天/前天/N小时前/N天前），别用 URL 年月糊一个日期，会错位。

## 常用任务


| 想做           | 怎么做                                                            |
| ------------ | -------------------------------------------------------------- |
| 加/改方向        | 编 `config/channels.yaml`（关键词）+ `judge.py` SYSTEM_PROMPT（白/黑名单） |
| 加 lab/作者徽章   | 编 `config/famous_labs.yaml`；常见名记得加 `require_affiliation`       |
| 调排序权重        | 编 `config/scoring.yaml` 的 points                               |
| 钉一篇论文        | `config/manual_arxiv.yaml` 贴 arxiv id（绕过 judge，永不下架）           |
| 强制重判某篇       | 删 `data/judge_cache.json` 里对应 entry，下次 build 重判                |
| 立刻刷新线上       | `gh workflow run daily.yml --ref main`                         |
| 推代码（push 挂时） | `python scripts/api_push.py`（推 main）或 gh API 建分支               |


## Paper 数据模型（`models.py`）

每篇论文一个 `site/data/papers/{id}.json`。`id` 形如 `arxiv-2606-12366`（点换横线）。核心字段：`title/title_zh/abstract/abstract_zh/tldr_zh/cover_zh`、`authors[{name,affiliation}]`、`channels[]`、`badges[{kind,label}]`、`score/score_breakdown`、`judge{relevant,research_value,primary_channel,reason}`、`institutions[]/method_tags[]/platform[]/sim_stack[]/method_family/real_robot/training_summary`（enrich 7 字段）、`demo_videos[]`。`from_dict` 会丢弃未知键，模型可向后兼容演进。