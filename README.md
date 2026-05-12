# redpaper

> 把每天看不完的论文整理成「小红书 feed」的静态站。
> arXiv + 中文公众号 → DeepSeek 把关 + 抽机构 / 方法 tag → 中文翻译 → 渲染 PDF 首页 → GitHub Pages 部署。
> 全部跑在 GitHub Actions 免费层，零服务器、零运营成本。

在线 Demo：[https://Nangongyeee.github.io/redpaper/](https://Nangongyeee.github.io/redpaper/)

---

## 工作流

每天北京时间 07:00，GitHub Actions 自动跑一次完整流水线。10 步走完：

```text
┌── ① 抓取 ────────────────────────────────────────────────┐
│  arXiv API（近 2 天 + 不够就回补 30 天 evergreen）        │
│  + 量子位 / 具身智能之心 / 深蓝具身智能（公众号镜像）     │
│  + config/manual_arxiv.yaml（站长钉论文，绕过判官）       │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ② 关键词过滤 (config/channels.yaml) ───────────────────┐
│  标题/摘要命中任一 keyword，且不命中 exclude → 入候选池   │
│  ⚠ 关键词宁多勿少，后面有 LLM 把关，漏召回比误召回更糟    │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ③ 🛡 DeepSeek V4-Flash 质量门禁 (judge.py) ─────────────┐
│  输入 ：标题 + 摘要                                       │
│  输出 ：{ relevant, research_value, primary_channel,      │
│            reason(中文 30 字) }                            │
│  relevant=false → 砍掉，写 tmp/judge-drops.md             │
│  → 结果缓存 data/judge_cache.json（同一篇不重复付费）     │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ④ 🏷 DeepSeek V4-Flash 抽二级 tag (enrich.py) ──────────┐
│  抽 ≤3 个机构（MIT / Boston Dynamics / 宇树 …）           │
│  + ≤3 个方法/问题 tag（DAgger / VAE / sim2real / 特技…）  │
│  → 缓存 data/enrich_cache.json                             │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑤ ✍ LLM 翻译标题 + 摘要 + TL;DR (translate.py) ─────────┐
│  后端链：gemini → deepseek → openai → dryrun（依次兜底）  │
│  已翻译的跳过（site/data/papers/{id}.json 存盘）          │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑥ 🖼 渲染封面 (render.py) ──────────────────────────────┐
│  下载 PDF → PyMuPDF 转 JPG → 取首页 + 第 2/3/4 页内页轮播  │
│  存 site/assets/img/covers/{id}.jpg                       │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑦ ⚡ 评分 (scoring.py) ─────────────────────────────────┐
│  顶会(ICRA/IROS/CoRL/RSS) + 知名 lab + HF Daily 上榜 +     │
│  Semantic Scholar 高引 + 长 paper + 来自公众号策展加分    │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑧ 📦 写产出 (build.write_feed) ─────────────────────────┐
│  site/data/index.json       主 feed（按分数倒序）          │
│  site/data/daily/*.json     每日归档                       │
│  site/data/papers/*.json    每篇完整数据                   │
│  site/data/channels.json    主页 tab 元数据                │
│  site/rss.xml               RSS 订阅                       │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑨ 🔖 cache-busting (build.stamp_assets) ────────────────┐
│  给所有 HTML/JS 链接加 ?v=<git-sha>                       │
│  保证 Pages 部署后浏览器不会卡在旧版数据                  │
└──────────────────────────────────────────────────────────┘
                            ↓
┌── ⑩ 🌐 部署 (.github/workflows/daily.yml) ────────────────┐
│  Actions commit 生成的 data → push main → Pages 自动发布   │
└──────────────────────────────────────────────────────────┘
                            ↓
                  📱 你打开 redpaper.github.io
```

**三层缓存**贯穿全程，让重跑近乎免费：


| 缓存文件                         | 谁写           | 命中后省什么            |
| ---------------------------- | ------------ | ----------------- |
| `data/judge_cache.json`      | judge.py     | DeepSeek 判官调用     |
| `data/enrich_cache.json`     | enrich.py    | DeepSeek 抽 tag 调用 |
| `site/data/papers/{id}.json` | translate.py | LLM 翻译 + PDF 重渲   |


跑完一轮的全部产出（commit 回 git）：

- `site/data/index.json` — 主 feed
- `site/data/daily/YYYY-MM-DD.json` — 每日归档
- `site/data/papers/{id}.json` — 每篇论文完整数据
- `site/assets/img/covers/{id}.jpg` + `-p2/p3/p4.jpg` — 首页 + 内页预览
- `site/rss.xml` — RSS 订阅
- `tmp/judge-drops.md`（本地 audit 时）— 被 LLM 砍掉的清单

---

## 把这个仓库当成「论文 feed 模板」克隆走，5 步就能跑出一个**只推你方向论文**的私人小红书。

### 1. Fork / 用作 Template

GitHub 右上角 **Use this template** → Create a new repository。
建议仓库名直接叫 `redpaper`，这样 Pages URL 是 `https://你的用户名.github.io/redpaper/`，否则要改 `<base href>`。

### 2. 在 Settings → Secrets 加 LLM Key

至少加一个：


| Secret             | 用途                                            | 申请                                                     |
| ------------------ | --------------------------------------------- | ------------------------------------------------------ |
| `DEEPSEEK_API_KEY` | 推荐：**判官 + 抽 tag** 都靠它；翻译也能兜底。 国内免费充值 ¥10 够跑半年 | [platform.deepseek.com](https://platform.deepseek.com) |
| `GEMINI_API_KEY`   | 可选：翻译首选，免费 1500 次 / 天                         | [aistudio.google.com](https://aistudio.google.com)     |
| `OPENAI_API_KEY`   | 可选：兜底；可配 `OPENAI_BASE_URL` 走中转                | —                                                      |


只配 `DEEPSEEK_API_KEY` 也能跑全套，不会卡住。

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

模板自带 5 个「人形机器人」频道（Loco-Manip&WBC / Manipulation / Teleoperation / Locomotion / Sim-to-Real），可以全删了换成自己的。**关键词宁多勿少**——后面有 LLM 把关，宁可错召回也不要漏。

### 4. 改 `scripts/redpaper/judge.py` —— 教 LLM 替你审稿

`SYSTEM_PROMPT` 里有两段：

```text
用户关心的方向（5 大频道...）：
  - loco-manip-wbc：人形机器人全身控制 + 移动操作...
  - manipulation：灵巧手、抓取...
  ...

**不要**接受的：
  - 纯医疗 / 手术 / 康复机器人
  - 公司融资 / 招聘 / 行业沙龙活动
  ...

**接受**的：
  - 真机器人上跑的 RL / IL / VLA
  - 知名实验室（Boston Dynamics, Figure, 1X, 宇树, 智元...）
  ...
```

把这段改成**你方向的白名单 + 黑名单**就行。LLM 会按这个 prompt 给每篇 paper 打 `relevant: true/false` + 一句中文 reason，砍掉的直接不上站。

> 这一步可以让站子从「关键词召回的垃圾堆」变成「值得每天打开看的高质量 feed」。

### 5. 改 `config/site.yaml` —— 站名、主色

```yaml
site:
  title: redpaper
  subtitle: 你想要的副标题
  author: 你的名字
  primary_color: "#FF2442"   # 卡片高亮色
  feed_page_size: 60
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

之后每天北京时间 07:00 自动跑，commit 数据 + 部署一气呵成。

---

## 本地运行

```bash
git clone <your-fork>
cd redpaper
pip install -r scripts/requirements.txt

# A) 最快：不调 LLM，英文照搬，验证流水线能跑通
REDPAPER_LLM_BACKEND=dryrun python scripts/build.py

# B) 完整体验：DeepSeek 一把梭（判官 / enrich / 翻译都用它）
DEEPSEEK_API_KEY=sk-xxx python scripts/build.py

# C) 翻译给 Gemini，判官 / enrich 仍走 DeepSeek
GEMINI_API_KEY=xxx DEEPSEEK_API_KEY=sk-xxx \
  REDPAPER_LLM_BACKEND=gemini python scripts/build.py

# 本地预览（cache-bust 自动起效）
cd site && python -m http.server 8000
# 浏览器打开 http://localhost:8000
```

`REDPAPER_LLM_BACKEND` 控制**翻译**走哪个后端；判官 / enrich 永远用 `DEEPSEEK_API_KEY`。

---

## 仓库结构

```
config/                  # 你能改的所有东西都在这
  channels.yaml          # 5 大频道 + 关键词
  sources.yaml           # 各数据源开关
  site.yaml              # 站名 / 主色 / 翻译后端
  manual_arxiv.yaml      # 站长精选钉论文
  famous_labs.yaml       # 名 lab / 大佬识别规则（卡片上加徽章）

scripts/
  build.py               # 流水线入口
  import_awesome.py      # 批量喂 awesome 仓库
  migrate_channels.py    # 频道字段重映射 + 补 chip
  audit_judge.py         # 对已有 paper 跑一遍 judge（站点清洗）
  api_push.py            # git push TLS 失败时的兜底（gh api 推送）
  retranslate_news.py    # Gemini 限额时分批重译
  redpaper/
    config.py            # YAML → dataclass
    models.py            # Paper / Author 数据结构
    sources/             # arxiv / hf_daily / qbitai / cn_news / manual_arxiv ...
    judge.py             # ✨ DeepSeek 把关
    enrich.py            # ✨ 机构 + 方法 tag 抽取
    translate.py         # LLM 翻译抽象层
    render.py            # PDF → JPG 封面
    scoring.py           # 卡片"为啥今天选了它"的评分系统
    labs.py              # 名 lab 徽章识别
    digest.py            # RSS / Markdown digest 输出
    build.py             # 主编排：fetch → judge → enrich → translate → render

site/                    # GitHub Pages 服务的全部静态产物
  index.html             # 瀑布流首页
  post.html              # 详情页（含 PDF 多页轮播）
  archive.html           # 按日期归档
  favorites.html         # 浏览器本地收藏夹
  rankings.html          # 总 / 周 / 月排行榜
  about.html             # 关于 / 配置展示
  assets/{css,js,img}/
  data/                  # 流水线生成
    index.json           # 主 feed
    site.json            # 站名 / 颜色 / crawl meta
    channels.json        # 频道 tab 元数据
    days.json            # 归档页索引
    daily/YYYY-MM-DD.json
    papers/{id}.json     # 每篇论文完整数据
  rss.xml

.github/workflows/
  daily.yml              # 每天 23:00 UTC（北京 07:00）跑流水线 + 推数据
  deploy.yml             # 任何 push 都重新部署
```

---

## 常用脚本


| 命令                                             | 作用                                                         |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `python scripts/build.py`                      | 完整跑一轮：抓取 → 判官 → enrich → 翻译 → 渲染 → 写 feed                  |
| `python scripts/audit_judge.py --apply`        | 对已上站论文全跑一遍 judge，砍掉不达标的，写 `tmp/judge-drops.md`             |
| `python scripts/import_awesome.py --year 2026` | 从 `awesome_papers.md` 批量导入指定年份的论文                          |
| `python scripts/migrate_channels.py --apply`   | 改完 channels.yaml 后跑一次，把已有 paper 的 channels 字段重映射 + 关键词扫描补充 |
| `python scripts/api_push.py`                   | `git push` 因 TLS 失败时的兜底（走 gh api）                          |


---

## 成本估算（DeepSeek V4-Flash + V3）


| 阶段              | 模型               | 单价               | 单篇 token           | 单篇成本        |
| --------------- | ---------------- | ---------------- | ------------------ | ----------- |
| 判官 (judge)      | V4-Flash         | ¥0.5 / 1M tok in | ~500 in + ~150 out | **¥0.0005** |
| 二级 tag (enrich) | V4-Flash         | 同上               | ~500 in + ~100 out | **¥0.0005** |
| 翻译 (translate)  | V3 / Gemini Free | ¥1 / 1M tok（或免费） | ~600 in + ~500 out | **¥0.0011** |


每天 30 篇新论文 × 三步 ≈ **¥0.06 / 天**，**¥2 / 月**。配上 Gemini 免费层兜底，常态下连一毛钱都不到。

> 缓存命中后**完全不掏钱** —— 判官 / enrich / 翻译都写盘缓存（`data/judge_cache.json` / `data/enrich_cache.json` / `site/data/papers/*.json`），同一篇 paper 重跑只读盘。

---

## 设计取舍

- **静态 + 客户端**：所有交互（收藏夹、分类、深色模式）走 `localStorage`，没有用户体系，没有后端。换设备 = 换收藏。
- **LLM 把关 > 关键词**：关键词召回粗放（必须粗放才不漏），靠 DeepSeek 在入站前做一次「相关性 + 价值」二审。漏掉一篇 ≈ 没事，混进一篇水货 ≈ 看着烦——所以宁缺勿滥。
- **数据走 git**：所有 paper json / 封面 jpg 都 commit 进仓库，借 GitHub LFS 友好的层级（每篇 ~5KB JSON + ~50KB 封面）。1000 篇 ≈ 50MB，能扛好几年。
- **Cache-busting 治本**：每次 build 用 git short-SHA 给 HTML / JS 打 `?v=...` 戳，浏览器永远不会卡在旧版数据上。

---

## 路线图

- 5 大频道 + LLM 把关 + 机构/方法二级 tag + awesome 批量导入
- DeepSeek-V4-Flash 判官 + V3 翻译双后端
- PDF 多页预览、KaTeX、本地收藏分类、深色模式
- 总 / 周 / 月排行榜
- Mascot Live2D 立绘开关
- HF Daily / Semantic Scholar 热度自动接入
- 邮件 / 飞书每日推送
- 多人共享收藏（不打算做——会破坏「私人 feed」的纯粹）

---

## License

MIT。fork 随意，PR 欢迎。