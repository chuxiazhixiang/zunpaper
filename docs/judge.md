# DeepSeek 论文质量门禁 (judge)

> 关键词命中 ≠ 真相关。命中 humanoid / VLA / 具身 等词只能保证「字面相关」，
> 不代表「对你科研有帮助」。这层 LLM 门禁的目的就是：在 paper 上站之前
> 让 DeepSeek 替你过一遍。**relevant=False 的不上首页。**

---

## 1. 模型选择：为什么用 V4-Flash

DeepSeek 在 2026-04 把模型矩阵改成了两挡：

| 模型 ID | 总参 / 激活 | 输入 ($/M tok) | 输出 ($/M tok) | 适合场景 |
|---|---|---|---|---|
| `deepseek-v4-flash` | 284B / 13B | **0.14** | **0.28** | 分类、判定、翻译、抽取 |
| `deepseek-v4-pro`   | 1.6T / 49B | 0.435（促销）/ 1.74（标价） | 0.87 / 3.48 | 数学、复杂推理、长链 CoT |

> 缓存命中输入价更便宜：V4-Flash $0.0028 / 1M。  
> 兼容性别名：`deepseek-chat` / `deepseek-reasoner` 在 2026-07-24 之前继续可用，
> 之后将关闭，迁移到上面的新 ID 即可。

判定任务是简单分类（relevant=true/false + 三档 value）—— **V4-Flash 完全够用，
价格只有 V4-Pro 的 1/3，延迟一半**。CoT reasoning 没必要打开，我们在 payload 里
显式关掉：

```python
payload["thinking"] = {"type": "disabled"}
```

（之前没关 reasoning 时，`completion_tokens_details.reasoning_tokens` 单次能吃掉
160 tokens，导致 `max_tokens=200` 装不下完整 JSON 而被截断。关掉后单次响应稳定
在 80-120 tokens。）

---

## 2. 怎么调（推荐两种写法）

### A. OpenAI SDK（最简）

```python
import os
from openai import OpenAI

client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                base_url="https://api.deepseek.com")

resp = client.chat.completions.create(
    model="deepseek-v4-flash",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user",   "content": f"标题：{title}\n\n摘要：{abstract}"},
    ],
    response_format={"type": "json_object"},
    temperature=0.0,
    max_tokens=400,
    extra_body={"thinking": {"type": "disabled"}},
)
print(resp.choices[0].message.content)
```

### B. raw HTTP（本仓库 `scripts/redpaper/judge.py` 用的方式）

```python
import requests
r = requests.post(
    "https://api.deepseek.com/chat/completions",
    headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    json={
        "model": "deepseek-v4-flash",
        "messages": [...],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 400,
        "thinking": {"type": "disabled"},
    },
    timeout=60,
)
data = r.json()["choices"][0]["message"]["content"]
```

两种写法的请求体完全一致，OpenAI SDK 只是个 wrapper。

---

## 3. 输出 schema

```json
{
  "relevant": true,                        // 是否值得上 redpaper
  "research_value": "high|medium|low",     // 对一线科研者的参考价值
  "primary_channel": "whole-body|loco-manip|locomotion|manipulation|vla|none",
  "reason": "20-40 字中文简评，说明为什么留 / 砍"
}
```

`Paper.judge` 会原样存这四个字段 + 用了哪个模型。前端可以选择在详情页
渲染 `reason` 作为「LLM 评论」展示。

---

## 4. Prompt（节选）

完整版见 `scripts/redpaper/judge.py:SYSTEM_PROMPT`。要点：

- 列举了用户关心的 5 个 channel（whole-body / loco-manip / locomotion /
  manipulation / vla）+ 各自典型话题
- 给出**不要的**类目：医疗手术、产线 SCARA、融资八卦、自驾、AIGC 漫剧、
  LLM-only agent ...
- 给出**接受的**类目：真机器人 RL/IL/VLA、仿真器/数据集/benchmark、
  顶尖实验室公司（Boston Dynamics / Figure / 1X / 宇树 / 智元 / 银河通用 /
  Physical Intelligence / BAIR / NVIDIA GEAR / Google DeepMind ...）的发布
- **宁缺勿滥原则**：拿不准 → false
- 黑名单关键词：「Video Friday」「Robot Talk」「Episode XX」等连载娱乐

---

## 5. 单月费用估算

### 单次调用

- 输入：~400 token（system prompt ~250 + paper title/abstract ~150）
- 输出：~80-120 token（关掉 reasoning 后）
- V4-Flash 价格：输入 $0.14 / M、输出 $0.28 / M

```
cost = 400 × 0.14 / 1e6  +  100 × 0.28 / 1e6
     ≈ 5.6e-5 + 2.8e-5
     ≈ 8.4e-5 USD
     ≈ ¥0.0006 / paper
```

### 缓存命中（system prompt 复用）

DeepSeek 自动对 prompt 前缀做 KV cache，命中后输入降到 $0.0028 / M：

```
cost ≈ 250 × 0.0028 / 1e6  +  150 × 0.14 / 1e6  +  100 × 0.28 / 1e6
     ≈ 0.7e-6 + 2.1e-5 + 2.8e-5
     ≈ 5.0e-5 USD
     ≈ ¥0.00035 / paper（命中后）
```

我们的 system prompt 是稳定不变的 → 实际付费基本走「缓存命中」。

### 单月总花费

| 场景 | 单日新文章数 | 单月成本（无缓存） | 单月成本（缓存命中） |
|---|---|---|---|
| 节制（仅你关心的方向） | 30 | ¥0.55 | ¥0.32 |
| 当前 | ~60 | ¥1.1 | ¥0.63 |
| 极端（关键词大爆炸 + 多源） | 200 | ¥3.6 | ¥2.1 |

> 翻译走 V3 / V4-Flash + Gemini fallback，每篇约 ¥0.001（输入 ~300 + 输出 ~300）。
> 翻译月成本约 ¥0.5-1。**整个 LLM 链路单月预算 < ¥5**。

参考一次性回补审计的实际成本（2026-05-12）：

```
57 篇旧文章一次性 judge → $0.0048 ≈ ¥0.035
```

---

## 6. 缓存机制

- 路径：`data/judge_cache.json`（**仓库根**，不在 `site/` 下，不会被
  GitHub Pages 公开）
- 键：`paper.id`
- 值：完整的 `Judgment` + 时间戳
- 失效策略：目前只追加，不淘汰。如要重判，删掉对应条目或整文件即可。
- 已被 judge 砍掉的 paper 也会留在缓存里 —— 下次同 ID 出现时不会重复付费。

---

## 7. 开关

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | 必填。没有就跳过 judge（paper 直接 keep）。 |
| `REDPAPER_JUDGE_MODEL` | 默认 `deepseek-v4-flash`。可改 `deepseek-v4-pro` 但成本 ×3。 |
| `REDPAPER_JUDGE_DISABLE` | 设 `1` 完全关掉 judge 门禁。 |

`manual_pin` / `manual_arxiv` 来源的 paper 永远绕过 judge —— 站长手动钉的不
受 LLM 否决。

---

## 8. 一次性回补审计

新加的判定逻辑只对**新文章**生效。如果存量 paper 想重审，跑这个脚本：

```bash
# dry-run，只输出 tmp/judge-drops.md 报告
python scripts/audit_judge.py

# 执行删除，并重新生成 feed.json / digest / rss
python scripts/audit_judge.py --apply
```

报告示例（2026-05-12 第一次跑的结果）：

| | |
|---|---|
| 候选 | 58 |
| 通过 | 26 |
| 被砍 | 32 |
| API 调用 | 57 |
| 成本 | ¥0.035 |

被砍主要类别：
- Video Friday / Robot Talk 等娱乐栏目（6）
- 公司融资 / 量产 / 招聘新闻（5）
- 行业会议 / 访谈 / 政治新闻（6）
- 跑偏方向的 arXiv（6）
- 农业 / 笼统宏观新闻（5）
- 商业宣传 / 产品发布会（3）
- 其它（1，被 keep 但 value=low 留存复核）

---

## 9. 监控建议

- CI 每日跑 `build.py` 时会打印：`judge: %d called, %d cached, %d dropped as irrelevant`
- 如果某天 `dropped > called`，说明源里垃圾比例反常，可以人工 review 当天的
  `judge_cache.json` 新增条目，看是不是 prompt 飘了

---

## 10. 已知 caveat

1. **truncation bug** — 已修。最初没关 reasoning 时 V4-Flash 会偷偷做 CoT，
   吃掉 `max_tokens` 配额导致 JSON 被截断、解析失败、默认 false。
   现在 `thinking.type = "disabled"` + `max_tokens=400`，安全。
2. **保守倾向** — 当 abstract 信息不足（只有标题）时，LLM 倾向于砍。
   建议保证抓取阶段尽量带上首图描述 / 摘要 / TL;DR。
3. **DeepSeek API 限速** — 个人 key 默认 60 RPM。审计脚本默认 1.2s/call，
   一天的常规增量（10-50 篇）撑得起。
