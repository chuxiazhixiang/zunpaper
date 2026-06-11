# redpaper

> 把每天看不完的论文整理成「小红书 feed」的静态站。
> arXiv + 中文公众号 + 厂商 demo 视频 + LLM 联网发现 → DeepSeek 把关 + 抽 7 维结构化标签 → 中文翻译 → 渲染 PDF 首页 → 月度综述 → GitHub Pages 部署。
> 全部跑在 GitHub Actions 免费层，零服务器、零运营成本（LLM 一个月 ≈ ¥2）。

在线 Demo：[https://Nangongyeee.github.io/redpaper/](https://Nangongyeee.github.io/redpaper/)

---

## 工作流

每天 **UTC 03:00（北京 11:00）** GitHub Actions 自动跑一次完整流水线，UTC 06:00（北京 14:00）再兜底跑一次。

> **为什么是 11:00 而不是清早？** arXiv 周一到周五在美东 20:00 推送当天「新论文公告」（≈ 北京次日 08:00–09:00）。太早跑会稳定拿不到当天论文、日榜永远停在「昨天」。UTC 03:00 给 arXiv 写入扩散留了 2–3h buffer；14:00 那班是兜底，防 arXiv API 早高峰 429/503 或主 cron 没触发。

流水线主要步骤：

```text
┌── ① 抓取（多源汇流） ─────────────────────────────────────┐
│  • arXiv API（近 2 天，不够就回补 30 天 evergreen）        │
│  • 量子位 / 具身智能之心 / 深蓝具身智能（公众号镜像）       │
│  • 厂商 demo 视频：YouTube（BD/Unitree/Figure/1X…）+      │
│    Bilibili（宇树/智元/量子位…），每条包成一张卡           │
│  • 🔍 LLM 联网发现 (discover.py)：Gemini grounded search   │
│    主动补关键词漏召回的新论文，arxiv API 二次验真防幻觉    │
│  • config/manual_arxiv.yaml（站长钉论文，绕过判官）        │
└───────────────────────────────────────────────────────────┘
                            ↓
┌── ② 关键词过滤 (config/channels.yaml) ───────────────────┐
│  标题/摘要命中任一 keyword，且不命中 exclude → 入候选池   │
│  ⚠ 关键词宁多勿少，后面有 LLM 把关，漏召回比误召回更糟    │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ③ 标题级去重 ──────────────────────────────────────────┐
│  公众号偶尔同篇内容挂多个 URL（slug 哈希不同但标题一样）  │
│  → 归一化标题后只留一份                                   │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ④ 🛡 DeepSeek V4-Flash 质量门禁 (judge.py) ─────────────┐
│  输入 ：标题 + 摘要                                       │
│  输出 ：{ relevant, research_value, primary_channel,      │
│            reason(中文) }                                  │
│  relevant=false → 砍掉（manual_pin 的论文跳过判官）       │
│  → 结果缓存 data/judge_cache.json（同一篇不重复付费）     │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑤ 🏷 DeepSeek V4-Flash 抽 7 维结构化标签 (enrich.py) ───┐
│  institutions  机构（MIT / 宇树 / Figure …，≤3）          │
│  method_tags   方法+问题 tag（Diffusion / sim2real …，≤3）│
│  platform      硬件平台（Unitree G1 / Atlas / ALOHA …）   │
│  sim_stack     仿真栈（Isaac Lab / MuJoCo / Genesis …）    │
│  method_family 主方法家族（RL/IL/VLA/WorldModel/…）        │
│  real_robot    有没有真机实验（yes/no）                   │
│  training_summary  训练规模一句话（"100K human demos"…）  │
│  → 缓存 data/enrich_cache.json                             │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑥ 🎬 demo 视频抓取 (videos.py) ─────────────────────────┐
│  扫摘要 + 项目主页，命中 YouTube / Bilibili / mp4 链接    │
│  → 详情页内嵌播放，缓存 data/video_cache.json             │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑦ ✍ LLM 翻译标题 + 摘要 + TL;DR (translate.py) ─────────┐
│  后端链：gemini → deepseek → openai → dryrun（依次兜底）  │
│  已翻译的跳过（site/data/papers/{id}.json 存盘）          │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑧ 🖼 渲染封面 (render.py) ──────────────────────────────┐
│  下载 PDF → PyMuPDF 转 JPG → 取首页 + 第 2/3/4 页内页轮播  │
│  存 site/assets/img/covers/{id}.jpg                       │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑨ ⚡ 评分 (scoring.py / config/scoring.yaml) ───────────┐
│  manual_pin + 顶尖 lab + 关键作者 + HF Daily + 公众号策展  │
│  + 关键词密度 + 新鲜度 + 开源仓库 + 顶会 + 跨频道…加权求和 │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑩ 📦 写产出 (build.write_feed) ─────────────────────────┐
│  site/data/index.json       主 feed                       │
│  site/data/daily/*.json     每日归档                      │
│  site/data/papers/*.json    每篇完整数据                  │
│  site/data/channels.json    主页 tab 元数据               │
│  site/rss.xml               RSS 订阅                       │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑪ 📰 月度综述 (monthly_digest.py) ──────────────────────┐
│  DeepSeek 把当月收录的 paper + 视频写成 1500–2000 字综述  │
│  → site/data/digest/monthly/YYYY-MM.json + monthly.html   │
│  每天只重算「本月」，不重烧历史月份                       │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑫ 🔖 cache-busting (build.stamp_assets) ────────────────┐
│  给所有 HTML/JS 链接加 ?v=<git-sha>                       │
│  保证 Pages 部署后浏览器不会卡在旧版数据                  │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑬ 🌐 部署 (.github/workflows/daily.yml) ────────────────┐
│  Actions commit 生成的 data → push main → Pages 自动发布   │
└──────────────────────────────────────────────────────────┘
                            ↓
                  📱 你打开 redpaper.github.io
```

**多层缓存**贯穿全程，让重跑近乎免费（同一篇论文只判定 / 抽标签 / 翻译 / 渲染一次，结果 commit 回 git）：

| 缓存文件                         | 谁写              | 命中后省什么            |
| ---------------------------- | --------------- | ----------------- |
| `data/judge_cache.json`      | judge.py        | DeepSeek 判官调用     |
| `data/enrich_cache.json`     | enrich.py       | DeepSeek 抽标签调用    |
| `data/video_cache.json`      | videos.py       | demo 视频抓取         |
| `site/data/papers/{id}.json` | translate.py    | LLM 翻译 + PDF 重渲   |

跑完一轮的全部产出（commit 回 git）：

- `site/data/index.json` — 主 feed
- `site/data/daily/YYYY-MM-DD.json` — 每日归档
- `site/data/papers/{id}.json` — 每篇论文完整数据
- `site/data/digest/monthly/YYYY-MM.json` + `site/digest/monthly-YYYY-MM.md` — 月度综述
- `site/assets/img/covers/{id}.jpg` + `-p2/p3/p4.jpg` — 首页 + 内页预览
- `site/rss.xml` — RSS 订阅

---

## 把这个仓库当成「论文 feed 模板」克隆走，几步就能跑出一个**只推你方向论文**的私人小红书。

### 1. Fork / 用作 Template

GitHub 右上角 **Use this template** → Create a new repository。
建议仓库名直接叫 `redpaper`，这样 Pages URL 是 `https://你的用户名.github.io/redpaper/`，否则要改 `<base href>`。

### 2. 在 Settings → Secrets 加 LLM Key

至少加一个：

| Secret             | 用途                                                  | 申请                                                     |
| ------------------ | --------------------------------------------------- | ------------------------------------------------------ |
| `DEEPSEEK_API_KEY` | 推荐：**判官 + 抽标签 + 月度综述** 都靠它；翻译也能兜底。国内充值 ¥10 够跑半年 | [platform.deepseek.com](https://platform.deepseek.com) |
| `GEMINI_API_KEY`   | 可选：翻译首选 + **联网发现论文**（grounded search）必需，免费 1500 次/天 | [aistudio.google.com](https://aistudio.google.com)     |
| `OPENAI_API_KEY`   | 可选：翻译兜底；可配 `OPENAI_BASE_URL` 走中转                     | —                                                      |

只配 `DEEPSEEK_API_KEY` 也能跑全套（判官 / 标签 / 翻译 / 综述都用它），只是「LLM 联网发现新论文」这一步会自动跳过（它依赖 `GEMINI_API_KEY` 的真实联网能力）。

### 3. 改 `config/channels.yaml` —— 决定每天看到哪些方向

这是**最重要的一个文件**。每个 `channel` = 主页一个标签页，关键词命中任意一个就进站。

```yaml
channels:
  - id: my-direction              # URL 用，英文小写
    name: 我的方向                # 主页 tab 显示
    emoji: "🔥"
    arxiv_categories: [cs.RO, cs.LG]    # arXiv 大类（OR）
    keywords:                       # 标题或摘要含任一就命中（OR）
      - your keyword
      - 中文别名也行
      - "Boston Dynamics"           # 名 lab 名字
    exclude:                        # 含任一就丢
      - surgical
    max_per_day: 30                 # 软上限，超出按分数砍
```

模板自带 6 个「人形机器人」频道，可以全删了换成自己的：

| id | 标签 |
| --- | --- |
| `loco-manip-wbc` | Loco-Manipulation & Whole-Body Control（含**全身/人形 VLA**） |
| `manipulation` | Manipulation（含**纯机械臂/桌面操作 VLA**：OpenVLA / π0 / Octo / RDT…） |
| `teleop` | Teleoperation |
| `locomotion` | Locomotion |
| `world-model` | World Model（JEPA / Cosmos / Genie / Dreamer / 物理世界模型…） |
| `sim2real` | Sim-to-Real |

**关键词宁多勿少**——后面有 LLM 把关，宁可错召回也不要漏。

### 4. 改 `scripts/redpaper/judge.py` —— 教 LLM 替你审稿

`SYSTEM_PROMPT` 里描述了「用户关心的频道白名单」+「不接受 / 接受的内容黑白名单」。比如本仓库自带的规则里有一条很关键的 **VLA 按形态分流**：

```text
- 全身 / 人形 VLA（WholeBodyVLA、Figure Helix、1X NEO…）→ loco-manip-wbc
- 纯机械臂 / 桌面操作 VLA（OpenVLA、π0、Octo、RDT…）   → manipulation
- 泛化 generalist policy 默认归 manipulation，除非明确是人形全身
```

把这段改成**你方向的白名单 + 黑名单**就行。LLM 会按这个 prompt 给每篇 paper 打 `relevant: true/false` + `research_value` + `primary_channel` + 一句中文 reason，砍掉的直接不上站。

> 这一步可以让站子从「关键词召回的垃圾堆」变成「值得每天打开看的高质量 feed」。

### 5. 改 `config/site.yaml` —— 站名、主色、翻译后端

```yaml
site:
  title: redpaper
  subtitle: 你想要的副标题
  author: 你的名字
  primary_color: "#FF2442"   # 卡片高亮色
  feed_page_size: 60

translation:
  backend_env: REDPAPER_LLM_BACKEND   # gemini | deepseek | openai | dryrun
  default_backend: gemini
```

### 6. （可选）批量喂 awesome_papers 仓库

很多方向都有「awesome-XXX」论文清单。把内容存成 `awesome_papers.md`，然后：

```bash
DEEPSEEK_API_KEY=sk-xxx python scripts/import_awesome.py --year 2026
```

会把里面所有 2026 年的 arxiv 论文全部走一遍 judge + 翻译 + 入站，导入报告写到 `tmp/awesome-import-report.md`。

只想试一下：`--limit 5 --dry-run`。

### 7. （可选）手动钉论文

不想等爬虫？编辑 `config/manual_arxiv.yaml`，贴 arxiv 链接：

```yaml
papers:
  - id: 2401.12345
    channels: [my-direction]
    note: "组里在跟的工作，钉首页"
```

带 `manual_pin` 标签的论文会**绕过 judge** 直接上站，永不下架。

### 8. 启用 Pages + 第一次跑

1. Settings → Pages → **Source = GitHub Actions**
2. Actions 标签页 → **Daily build & deploy** → Run workflow
3. 等 10–15 分钟，打开 `https://你的用户名.github.io/redpaper/`

之后每天 UTC 03:00（北京 11:00）+ UTC 06:00（北京 14:00）兜底自动跑，commit 数据 + 部署一气呵成。

> ⚠️ GitHub Actions 的 schedule 在高峰期会有几十分钟到几小时的随机延迟。要立刻刷新可手动 `gh workflow run daily.yml --ref main`，10–15 分钟出新数据。

---

## 本地运行

```bash
git clone <your-fork>
cd redpaper
pip install -r scripts/requirements.txt

# A) 最快：不调 LLM，英文照搬，验证流水线能跑通
REDPAPER_LLM_BACKEND=dryrun python scripts/build.py

# B) 完整体验：DeepSeek 一把梭（判官 / enrich / 翻译 / 综述都用它）
DEEPSEEK_API_KEY=sk-xxx python scripts/build.py

# C) 翻译 + 联网发现给 Gemini，判官 / enrich 仍走 DeepSeek
GEMINI_API_KEY=xxx DEEPSEEK_API_KEY=sk-xxx \
  REDPAPER_LLM_BACKEND=gemini python scripts/build.py

# 本地预览（cache-bust 自动起效）
cd site && python -m http.server 8000
# 浏览器打开 http://localhost:8000
```

`REDPAPER_LLM_BACKEND` 控制**翻译**走哪个后端；判官 / enrich / 月度综述永远用 `DEEPSEEK_API_KEY`；联网发现永远用 `GEMINI_API_KEY`。

---

## 仓库结构

```
config/                  # 你能改的所有东西都在这
  channels.yaml          # 6 大频道 + 关键词
  sources.yaml           # 各数据源开关（arxiv / 公众号 / 视频 / discover…）
  site.yaml              # 站名 / 主色 / 翻译后端
  scoring.yaml           # 「为啥今天选了它」打分规则
  manual_arxiv.yaml      # 站长精选钉论文
  manual_xhs.json        # 手动挂的小红书 URL 列表
  famous_labs.yaml       # 名 lab / 大佬识别规则（卡片上加徽章）

scripts/
  build.py               # 流水线入口
  import_awesome.py      # 批量喂 awesome 仓库
  add_paper.py           # 单篇手动加论文
  migrate_channels.py    # 频道字段重映射 + 补 chip
  audit_judge.py         # 对已有 paper 跑一遍 judge（站点清洗）
  backfill_p1_p0.py      # 给存量论文回填 7 维标签 + demo 视频
  ingest_video_channels.py  # 单独跑一遍视频频道源
  run_monthly_digest.py  # 单独重算指定月份综述
  retranslate_news.py    # Gemini 限额时分批重译
  dev_run.py             # 本地开发快捷入口
  api_push.py            # git push TLS 失败时的兜底（gh api 推送）
  redpaper/
    config.py            # YAML → dataclass
    models.py            # Paper / Author 数据结构
    sources/             # arxiv / hf_daily / cn_news / manual_arxiv /
                         #   manual_xhs / semantic_scholar / video_channels
    judge.py             # ✨ DeepSeek 把关（相关性 + 科研价值）
    enrich.py            # ✨ 7 维结构化标签抽取
    discover.py          # 🔍 LLM 联网发现新论文（Gemini grounded search）
    videos.py            # 🎬 demo 视频抽取（YouTube / Bilibili / mp4）
    translate.py         # LLM 翻译抽象层
    render.py            # PDF → JPG 封面
    scoring.py           # 卡片"为啥今天选了它"的评分系统
    labs.py              # 名 lab 徽章识别
    digest.py            # RSS / Markdown digest 输出
    monthly_digest.py    # 📰 月度领域综述
    build.py             # 主编排：fetch → dedup → judge → enrich →
                         #   videos → translate → render → feed → 月报

site/                    # GitHub Pages 服务的全部静态产物
  index.html             # 瀑布流首页
  post.html              # 详情页（PDF 多页轮播 + demo 视频 + 图片放大）
  archive.html           # 按日期归档
  favorites.html         # 浏览器本地收藏夹
  rankings.html          # 日 / 周 / 月 / 总排行榜
  monthly.html           # 月度领域综述
  about.html             # 关于 / 配置展示
  assets/{css,js,img}/
    js/mascot.js         # 右下角 Live2D 站娘（眼睛跟随 + 随机表情）
    live2d/              # 自部署的 Live2D 运行时 + 模型
  data/                  # 流水线生成
    index.json           # 主 feed
    site.json            # 站名 / 颜色 / crawl meta
    channels.json        # 频道 tab 元数据
    days.json            # 归档页索引
    daily/YYYY-MM-DD.json
    papers/{id}.json     # 每篇论文完整数据
    digest/monthly/      # 月度综述 JSON
  rss.xml

.github/workflows/
  daily.yml              # UTC 03:00（北京 11:00）+ 06:00 兜底跑流水线 + 推数据
  deploy.yml             # site/** 有 push 就重新部署 Pages
```

---

## 常用脚本

| 命令                                             | 作用                                                         |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `python scripts/build.py`                      | 完整跑一轮：抓取 → 判官 → enrich → 翻译 → 渲染 → 写 feed → 月报             |
| `python scripts/audit_judge.py --apply`        | 对已上站论文全跑一遍 judge，砍掉不达标的                                    |
| `python scripts/import_awesome.py --year 2026` | 从 `awesome_papers.md` 批量导入指定年份的论文                          |
| `python scripts/migrate_channels.py --apply`   | 改完 channels.yaml 后跑一次，把已有 paper 的 channels 重映射 + 补充关键词扫描   |
| `python scripts/backfill_p1_p0.py`             | 给存量论文回填 7 维结构化标签 + demo 视频                                 |
| `python scripts/run_monthly_digest.py 2026-06` | 单独重算某个月份的领域综述                                              |
| `python scripts/api_push.py`                   | `git push` 因 TLS 失败时的兜底（走 gh api）                          |

---

## 成本估算（DeepSeek V4-Flash）

| 阶段              | 模型               | 单价               | 单篇 token           | 单篇成本        |
| --------------- | ---------------- | ---------------- | ------------------ | ----------- |
| 判官 (judge)      | V4-Flash         | ¥0.5 / 1M tok in | ~400 in + ~100 out | **¥0.0005** |
| 标签 (enrich)     | V4-Flash         | 同上               | ~400 in + ~100 out | **¥0.0005** |
| 翻译 (translate)  | Gemini Free / DeepSeek | 免费 / ¥1 / 1M | ~600 in + ~500 out | **≈0 / ¥0.001** |
| 月度综述 (monthly)  | V4-Flash         | 同上               | 每月一次 ~5K in + 2K out | **¥0.02 / 月** |

每天 ~30 篇新论文 × 判官+标签+翻译 ≈ **¥0.06 / 天**，配上激进缓存与 Gemini 免费翻译，实测**一个月 ≈ ¥2**。

> 缓存命中后**完全不掏钱** —— 判官 / 标签 / 视频 / 翻译都写盘缓存（`data/*.json` + `site/data/papers/*.json`），同一篇 paper 重跑只读盘。

---

## 设计取舍

- **静态 + 客户端**：所有交互（收藏夹、分类、深色模式、图片放大）走 `localStorage`，没有用户体系，没有后端。换设备 = 换收藏。
- **LLM 把关 > 关键词**：关键词召回粗放（必须粗放才不漏），靠 DeepSeek 在入站前做一次「相关性 + 价值」二审。漏掉一篇 ≈ 没事，混进一篇水货 ≈ 看着烦——所以宁缺勿滥。
- **联网发现补漏召回**：keyword 列表困死在「写过的词」上，新工作命名 / 新平台名召不回来。`discover.py` 让带 Google 搜索的 Gemini 主动补一层，候选 ID 全部走 arxiv API 验真，防 LLM 编号幻觉。
- **数据走 git**：所有 paper json / 封面 jpg 都 commit 进仓库（每篇 ~5KB JSON + ~50KB 封面）。1000 篇 ≈ 50MB，能扛好几年。
- **Cache-busting 治本**：每次 build 用 git short-SHA 给 HTML / JS 打 `?v=...` 戳，浏览器永远不会卡在旧版数据上。

---

## 路线图

已完成：

- 6 大频道 + LLM 把关 + 7 维结构化标签 + awesome 批量导入
- DeepSeek-V4-Flash 判官 / enrich / 月度综述 + 多后端翻译
- 厂商 demo 视频源（YouTube / Bilibili）+ 详情页内嵌播放
- LLM 联网发现新论文（Gemini grounded search）
- PDF 多页预览、KaTeX、图片放大、本地收藏分类、深色模式
- 日 / 周 / 月 / 总排行榜 + 月度领域综述
- Mascot Live2D 立绘（眼睛跟随 + 随机表情）

待办 / 想法：

- HF Daily / Semantic Scholar 热度自动接入（代码在，默认关）
- 每日 top 论文「全文精读卡片」（读 PDF 全文出结构化深读）
- 邮件 / 飞书每日推送
- 多人共享收藏（不打算做——会破坏「私人 feed」的纯粹）

---

## License

MIT。fork 随意，PR 欢迎。
