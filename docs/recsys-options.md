# 推荐系统选型小报告

> 目标：给 redpaper 加一层「按你的口味推荐」。
> 信号源：浏览器里的 ❤ 收藏 / 阅读记录 / 收藏分类，外加每篇论文的
> `score_breakdown`、`channels`、`abstract_zh`、`badges`。
> 约束：纯静态站，没有后端，只能在客户端跑或在 GitHub Actions 里离线算。

## TL;DR 推荐

**两阶段拼接最划算：**

1. **离线侧**（GitHub Actions）：用 `sentence-transformers` 把每篇论文的
   英文摘要 embed 成 384 维向量，存到 `site/data/embeddings.json`（约
   400 篇 × 384 × float16 ≈ 300 KB）。
2. **在线侧**（浏览器）：读 `localStorage` 里的收藏 ID，平均它们的 embedding
   得到「兴趣向量」，对全库算 cosine，叠加 `paper.score` 加权，排出 Top-N。

整套不依赖账号、不污染后端，5-10 秒就能搜完 500 篇。下面是更详细的方案对比。

---

## 候选方案矩阵

| 方案 | 信号 | 模型大小 | 计算位置 | 上手成本 | 适合 redpaper 吗 |
|---|---|---|---|---|---|
| **TF-IDF + cosine** | 词频 | < 1 MB | 浏览器 | ★☆☆ | ✅ 最简单的 baseline |
| **SBERT + cosine** | 语义 | embedding 300 KB | 浏览器 | ★★☆ | ✅✅ 推荐路线 |
| **LightFM** | 隐反馈 + 内容特征 | ~5 MB | 浏览器/CI | ★★★ | ⚠️ 单用户，协同信号≈0 |
| **implicit ALS** | 仅隐反馈 | ~5 MB | CI | ★★★ | ❌ 单用户没意义 |
| **Surprise** | 评分矩阵 | ~5 MB | CI | ★★★ | ❌ 同上 |
| **RecBole / DeepCTR** | 全套深度 RS | ~100 MB+ | GPU | ★★★★★ | ❌ 杀鸡用牛刀 |
| **「相关论文」启发式** | 频道交集 + 作者交集 | 0 | 浏览器 | ★☆☆ | ✅ 已部分实现 |

### 1. TF-IDF + cosine（最便宜）

```python
# scripts/embed_tfidf.py 之类
from sklearn.feature_extraction.text import TfidfVectorizer
vec = TfidfVectorizer(max_features=2048, stop_words="english")
X = vec.fit_transform([p.title + " " + p.abstract for p in papers])
# 存 X 跟词表
```

- 优点：纯标量数学，Python `scikit-learn` 一行；JS 端用 `tf-idf-search` 之类。
- 缺点：抓不到「diffusion policy ≈ visuomotor learning」这种语义近邻。
- redpaper 适用度：当 baseline 是好的，未来要加权可以直接当 fallback。

### 2. Sentence-BERT + cosine（推荐）

库：**`sentence-transformers`**（HuggingFace 出品，pip 即装）

```python
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("BAAI/bge-small-en-v1.5")   # 384 维，33MB，跑一次 ~30s/100 篇
emb = m.encode([p.abstract for p in papers], normalize_embeddings=True)
# emb.shape == (N, 384)
```

- 把 384 维 float16 写进 `site/data/embeddings.json`（或 `.bin` + base64）。
- 浏览器端用 `Float32Array`，500 篇 × 384 维 ~1MB，cosine 全量扫一次 < 5ms。
- 不需要 GPU，CI 跑一次写盘就行；每天 daily 增量再跑新论文。

> 模型选型：英文摘要用 `bge-small-en-v1.5` 或 `all-MiniLM-L6-v2`；
> 想中英文统一就用 `BAAI/bge-m3`（但有 1024 维，体积翻 2.5×）。

### 3. LightFM（hybrid）

- 用得着的场景：多用户网站，要把「协同过滤」和「内容特征」一起灌。
- redpaper 是单用户站，没有 user-paper 矩阵，LightFM 退化成纯内容侧，
  这种情况下不如 SBERT 简单。
- 跳过。

### 4. implicit ALS / Surprise / RecBole

全都是「需要 user × item 矩阵」的协同过滤，redpaper 没有多用户，pass。

### 5. 「相关论文」启发式（其实已经在用）

`post.js` 里 `relatedPapersHTML` 已经做了基础的：
- 同频道交集
- 作者交集
- 同时间窗

不需要 embedding 的话，把这个再调一调（加权 `score`，过滤已读）就比啥都没有强。
我建议先把 SBERT 路线落下来当主推荐，启发式留作备份。

---

## 推荐落地方案（如果要做）

**阶段 1：MVP（~2 小时）**

1. `scripts/embed_papers.py`：跑一次 `bge-small-en-v1.5`，输出
   `site/data/embeddings.json`（`{ "ids": [...], "vectors": [[...], ...] }`）。
   按 `paper.id` 对齐。
2. daily 工作流加一步「embed 新增 / 改动的论文」。
3. `site/assets/js/recsys.js`：
   - 读 `localStorage` 收藏 ID + 阅读记录 ID
   - fetch `data/embeddings.json`
   - 平均收藏 ID 的向量得到 `userVec`
   - 全库 cosine，叠加 `0.7 * cosine + 0.3 * (score/100)`
   - Top 12 渲染到首页一个新 section「猜你想看 ✨」
4. 没有收藏？fallback 到 score-top-N。

**阶段 2：交互细化（~1 小时）**

- 阅读后弹气泡问「这篇你喜欢吗？👍 / 👎」，把 👎 加入 `Hidden` 名单，
  下次推荐扣 0.3 cosine。
- 「不想看到」分类 = 把分类下的论文向量取负平均，从推荐打分里减。
- 推荐结果加可解释性 chip：「因为你收藏了《XXX》」（用最近邻匹配出来的）。

**阶段 3（可选，离线 fine-tune）**

- 累计 50+ 收藏后，可以用 SBERT 做 contrastive fine-tune：
  positive pair = (favorited, favorited)，negative = random。
- 跑一次 5-10 epochs，落 LoRA 参数，再 encode 全库。CI 跑 GPU 太贵，
  不推荐自动化；本地手动跑一次写盘就行。

---

## 结论

- **马上能用**：先把 score 排序（已实现）+ 「相关论文」启发式（已存在）做扎实，
  甚至不用上推荐模型，一个偏好排序就够 80% 体验。
- **下一步**：上 `sentence-transformers + bge-small-en` 的 embedding，
  300 KB 数据 + 50 行 JS 就把语义推荐做出来，免账号、免后端、零成本。
- **不要碰**：协同过滤类（implicit / LightFM / Surprise），单用户站没有协同信号。
