# auto_reply_influencer（足球网红评论引流工具）

把一份足球 Instagram 账号清单，自动变成一份"每天可手动发布"的 **EZCollegeApp** 推广评论任务单。

工具**不会自动发帖/评论**。发布和截图由你手动完成（封号风险最低）。工具负责最枯燥的部分：盯着 ~100 个账号、抓取最近的新帖、用 LLM 读懂图文，然后写出一条自然、贴合该帖内容的评论——用"足球 → 大学申请"的类比把 EZCollegeApp 自然植入（**只提品牌名，不带链接**）。

> 一句话：抓帖 → AI 写评论 → 生成每日任务单，你照着手动发。

---

## 🚀 快速上手（3 行命令，复制即用）

```bash
pip install -r requirements.txt                 # 首次：装依赖（默认 claude-cli 无需 API Key）
python run.py run --hours 2                      # 一键：抓帖 + AI 写评论 + 生成今日任务单
open "daily_tasks/$(date +%F)/tasks.md"         # 查看：照着里面的评论手动去发
```

发完一条后回记结果（可选）：
```bash
python run.py mark-done --post-id <帖子ID> --outcome survived --screenshot proof.png
```

**耗时**：一次 `run --hours 2`（全 100 账号）约 **10–25 分钟**——抓帖 ~6–10 分钟，生成评论 = 新帖数 × ~45 秒（2 小时窗口通常只有几条到十几条新帖）。想快近 10 倍可加 `--provider gpt`（需 `OPENAI_API_KEY`，按量计费）。
**结果在哪**：评论看 `daily_tasks/<日期>/tasks.md`；配图在 `media/<日期>/<账号>/`；数据库在 `data/influencer.db`。

---

## 流程总览

```
CSV ──▶ ①fetch ──▶ SQLite + media/   ──▶ ②generate ──▶ 评论存入数据库 ──▶ ③tasks ──▶ daily_tasks/<日期>/
        过去 N 小时    （自动去重）          LLM 读图文        （安全护栏）          你手动发布
```

1. **① fetch 抓帖** —— 对 CSV 里每个账号，用 Instagram 公开的 `web_profile_info` 接口（HTTP/2，**无需登录任何账号**；代理可选，不填则用本地 IP 直连）拉取最近的帖子，只保留**过去 N 小时**内发布的，下载配图并入库。重复运行**不会重复保存**同一条帖子（按 `post_id` 用 `INSERT OR IGNORE` 去重）。
2. **② generate 生成评论** —— 对每条还没评论的帖子，LLM 读取**配图 + 文案**，按既定风格写一条评论。默认用**终端里的 Claude CLI**（无需 API Key）；`gpt` 为第二选项，`gemini` 也支持。硬性护栏会**剔除任何链接**并限制长度。
3. **③ tasks 生成每日任务** —— 写出当天的 `daily_tasks/<日期>/` 任务包：一份可读的 `tasks.md`、一份 `tasks.json`，以及每条帖子一个文件夹（配图 + `comment.txt` 待发文案 + 给你放截图的 `screenshot.png` 位）。**同一条帖子最多只会进任务单一次**——绝不会让你对同一条帖子评论两遍。

---

## 安装

```bash
cd auto_reply_influencer
python3 -m venv .venv && source .venv/bin/activate      # 可选：建虚拟环境
pip install -r requirements.txt
cp .env.example .env        # IG_PROXY 可选：留空=本地 IP 直连；要稳定再填代理
```

默认的 LLM 提供方 `claude-cli` 需要系统 PATH 里有 `claude` 命令（你在用 Claude Code，所以已具备），且**不需要任何 API Key**。
若要改用 `gpt` / `gemini`：在 `requirements.txt` 里取消对应行的注释、`pip install`，并在 `.env` 里填好对应的 Key。

---

## 📦 仓库内容说明（给接手的同事）

为安全和整洁，本仓库**只含代码与配置，不含密钥和运行产物**。以下目录/文件被 `.gitignore` 排除，**不在仓库里，首次运行会自动生成**：

| 未上传 | 是什么 | 怎么得到 |
|---|---|---|
| `.env` | 密钥（**可选** `IG_PROXY` 代理、各家 API Key） | `cp .env.example .env`；`IG_PROXY` 留空=用本地 IP，代理可向上游负责人索取 |
| `data/` | SQLite 数据库（帖子/任务/去重/存活统计） | 运行 `fetch` 自动创建 |
| `media/` | 抓取到的帖子配图 | 运行 `fetch` 自动下载 |
| `daily_tasks/` | 每日待发评论任务单（**最终结果**） | 运行 `tasks` / `run` 自动生成 |

**接手三步走**：

```bash
pip install -r requirements.txt          # 1. 装依赖
cp .env.example .env                     # 2. IG_PROXY 可选（留空=本地 IP 直连，建议先这样测）
python run.py run --hours 2              # 3. 一键跑通，结果在 daily_tasks/<日期>/tasks.md
```

> 改提示词风格 → 改 `prompts/comment_prompt.md` 和 `prompts/examples.md`（无需动代码）。
> 改参数（时间窗口、每日条数、模型等）→ 改 `config.yaml`。
> 代码分四块：`core/ig_fetcher.py`(抓帖) · `core/llm_client.py`(多模型客户端) · `core/comment_generator.py`(写评论+护栏) · `core/task_writer.py`(任务单)，入口是 `run.py`。

---

## 使用方法（命令）

```bash
# ① 抓帖（幂等，可放进 cron 每 2 小时跑一次）
python run.py fetch --hours 2                 # 抓全部账号、过去 2 小时
python run.py fetch --hours 48 --limit 5      # 只抓前 5 个账号、放宽到 48 小时（测试用）

# ② 给"还没有评论"的帖子生成评论
python run.py generate                        # 用默认提供方（claude-cli）
python run.py generate --provider gpt --limit 3   # 改用 GPT，只处理 3 条

# ③ 生成当天的任务单
python run.py tasks

# 一条命令跑完三步
python run.py run --hours 2 --max-tasks-per-day 20

# 你手动发完一条评论后，回记结果（驱动实验复盘）
python run.py mark-done --post-id 1234567890 --outcome survived --screenshot proof.png
#   --outcome 取值：survived(还在) | hidden(被折叠) | removed(被删)

# 查看统计（帖子数 / 任务状态 / 评论存活情况）
python run.py stats
```

### 各参数说明

| 参数 | 含义 | 默认 |
|---|---|---|
| `--hours N` | 只保留过去 N 小时内发布的帖子 | `config.yaml` 里的 `time_window_hours`（2） |
| `--limit N` | 只处理前 N 个账号 / N 条帖子（测试用） | 全部 |
| `--provider` | `claude-cli` / `claude-api` / `gpt` / `gemini` | `claude-cli` |
| `--max-tasks-per-day N` | 每天最多生成多少条评论任务（防封节流） | 20 |
| `--csv` | 覆盖 CSV 路径 | `config.yaml` 里的 `csv_path` |

---

## 推荐的"实验策略"流程

1. **建立基线**：先跑一次 `fetch`，把现有帖子记录在案（只有之后**新发**的帖子才会进任务单）。
2. **小批量验证**：先用 `--limit 5` 在头部账号上跑，肉眼检查生成的评论 + 配图相关性是否过关。
3. **手动发 + 记结果**：手动发一批，之后 24–48 小时内用 `mark-done` 回记每条的存活情况。
4. **看存活率、调提示词**：在 `data/influencer.db` 的 `tasks` 表里按账号 `Type` / 提供方统计存活率，再去微调 `prompts/comment_prompt.md`。
5. **稳了再放量**：存活率健康后，用较小的 `--max-tasks-per-day` 安排 `run` 每 2 小时跑一次（cron / `/loop`）。

> ⚠️ 现实提醒：在 C 罗（6.69 亿粉）这种超大账号下发品牌评论，曝光极高，即便不带链接也要预期**部分会被折叠**。所以第一周请把它当"测量期"，不要急着放量——`mark-done` 的存活率统计就是为此而生。

---

## 目录结构

| 路径 | 作用 |
|---|---|
| `run.py` | 命令行总入口（fetch / generate / tasks / run / mark-done / stats） |
| `config.yaml` / `.env` | 配置 / 密钥 |
| `core/ig_fetcher.py` | ① 抓帖（`web_profile_info` + 代理） |
| `core/store.py` | SQLite（`posts`、`tasks` 两张表）+ 配图下载 + 去重 |
| `core/llm_client.py` | 多模态、多提供方 LLM 客户端 |
| `core/comment_generator.py` | 提示词拼装 + 安全护栏 |
| `core/task_writer.py` | ③ 每日任务包生成 |
| `prompts/examples.md` | 优良评论的 few-shot 范例 |
| `prompts/comment_prompt.md` | 生成评论用的提示词 |
| `data/`、`media/`、`daily_tasks/` | 运行产物（已被 gitignore） |

---

## 安全与防封说明

- **读帖**无需登录、风险低；**发帖/评论**才是高风险动作，所以发布环节保持**手动**。
- 评论默认**不带任何链接**（`config.yaml` 里 `allow_link: false`）——链接是评论被删、账号被标记的头号诱因。请把 EZCollegeApp 的链接放在**个人主页简介**里。
- 把 `max_tasks_per_day` 设小一点，避免短时间内从同一个账号集中发出大量品牌评论。

---

## 提示词与风格（可自行修改）

评论风格的"教学样本"在 `prompts/examples.md`，包含两类：
- **A 组**：你真实发过的评论（即 `example/*.jpg` 截图里的那种——亲和、像在"推荐一个工具"，但**新版不再带链接**）。
- **B 组**：足球 → 大学申请的类比金句（来自 `example/Promo Posts.docx`），这是当帖子内容是"足球"而非"升学"时把话题自然拐到 EZCollegeApp 的关键手法。

生成规则写在 `prompts/comment_prompt.md`：一个开头 emoji、先就帖子本身真实地评论一句、再用与账号类型匹配的足球类比过渡、自然提一次 EZCollegeApp、**不带链接、不用缩写、≤160 字符、最多 2 个话题标签**。想调整口吻直接改这两个文件即可，无需改代码。

---

## 常见问题

- **想从头开始 / 清空数据？** 删除 `data/`、`media/`、`daily_tasks/` 三个目录即可（都已 gitignore）。
- **抓帖报大量限流 / 失败？** 默认用**本地 IP 直连**；用本地 IP 抓满 100 个账号容易被限流——可先 `--limit` 小批量测试，或在 `.env` 配置 `IG_PROXY` 代理走住宅 IP。
- **想换模型？** `generate --provider gpt`（需 `OPENAI_API_KEY`）或 `--provider gemini`（需 `GEMINI_API_KEY`）；`claude-cli` 不可用时会自动回退到 `claude-api`（需 `ANTHROPIC_API_KEY`）。
