# redpaper

把每日 AI 论文做成「小红书 feed」的小项目。每天定时抓 arXiv 等源、用 LLM 翻译标题与摘要、渲染 PDF 首页，最后用瀑布流呈现。

部署在 GitHub Pages，无需后端，零运营成本。

## 在线访问

启用 GitHub Pages 后访问：`https://Nangongyeee.github.io/redpaper/`
（仓库 Settings → Pages → Source = `GitHub Actions`）

## 仓库结构

```
config/                # 频道 / 数据源 / 站点配置（你可改）
  channels.yaml        # 频道与关键词
  sources.yaml         # 各数据源的开关
  site.yaml            # 站点元信息、LLM 后端、缓存路径
scripts/
  build.py             # 入口
  redpaper/            # 流水线代码
    sources/arxiv_source.py
    render.py          # PDF 首页转 JPG
    translate.py       # LLM 抽象层（gemini / deepseek / openai / dryrun）
    build.py           # 编排
site/                  # GitHub Pages 服务的静态站
  index.html           # 瀑布流首页
  post.html            # 详情页
  favorites.html       # 收藏夹
  archive.html         # 每日归档
  about.html           # 关于
  assets/{css,js,img}/
  data/                # 流水线生成（index.json, daily/*.json, papers/*.json）
.github/workflows/
  daily.yml            # 每天 08:00 UTC+8 自动跑
  deploy.yml           # 推送到 main 自动重新部署
```

## 本地运行

```bash
pip install -r scripts/requirements.txt

# 不调 LLM，直接用英文占位（最快本地预览）
REDPAPER_LLM_BACKEND=dryrun python scripts/build.py

# 或者用 Gemini 免费层（境外网络环境）
GEMINI_API_KEY=xxx REDPAPER_LLM_BACKEND=gemini python scripts/build.py

# 或者 DeepSeek
DEEPSEEK_API_KEY=xxx REDPAPER_LLM_BACKEND=deepseek python scripts/build.py

# 本地预览
cd site && python -m http.server 8765
# 浏览器打开 http://localhost:8765
```

## LLM 后端

`scripts/redpaper/translate.py` 支持四种后端，用 `REDPAPER_LLM_BACKEND` 切换：

| backend  | 需要的环境变量           | 说明                                       |
| -------- | ------------------------ | ------------------------------------------ |
| dryrun   | (无)                     | 不调 API，英文照搬。本地开发最快           |
| gemini   | `GEMINI_API_KEY`         | Google Gemini 2.0 Flash，免费层每天 1500 次 |
| deepseek | `DEEPSEEK_API_KEY`       | DeepSeek Chat，OpenAI 兼容协议，国内可用    |
| openai   | `OPENAI_API_KEY` + 可选 `OPENAI_BASE_URL` / `OPENAI_MODEL` | 任何 OpenAI 兼容端点 |

把对应 key 配到仓库 Settings → Secrets，CI 会自动用上。

## 配置频道

改 `config/channels.yaml`：

```yaml
channels:
  - id: my_channel
    name: 我的方向
    emoji: "🔥"
    arxiv_categories: [cs.CL, cs.LG]
    keywords:
      - your keyword
    max_per_day: 30
```

`keywords` OR 关系；留空表示该 arXiv 分类下任何论文都收。

## 数据源（按阶段启用）

`config/sources.yaml` 里逐个开关：

- **arxiv**：Phase 1 已启用，主数据源。
- **hf_daily / semantic_scholar / qbitai / jiqizhixin / synced_review / alphaxiv**：Phase 2，会在后续 PR 接入。
- **manual_xhs**：Phase 3，手动维护小红书链接列表 `config/manual_xhs.json`。

## 路线图

- **Phase 1 (已完成)**：arXiv + 翻译 + 瀑布流 + 详情页 + 本地收藏 + 暗色模式 + Actions 部署
- **Phase 2**：HF Daily / Semantic Scholar 热度徽章；量子位 / 机器之心 / 新智元 资讯抓取与匹配；多频道；归档页；KaTeX 公式渲染
- **Phase 3**：大佬过滤、相关论文、每日推送（飞书 / 邮件 / RSS）、手动小红书源
