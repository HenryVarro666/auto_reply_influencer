# 设计:抓帖阶段新增 4 种数据源(关键词 / 标签 / 单账号 / 单帖)

- 日期:2026-06-29
- 状态:已批准(整体方案),待 spec 评审
- 影响范围:仅 Stage 1「抓帖」的数据来源。Stage 2(写评论)、Stage 3(任务单)不改。

## 1. 背景与目标

当前 `auto_reply_influencer` 只能从一份 CSV 账号清单出发,逐账号用免登录的
`web_profile_info` 接口拉取「过去 N 小时」内的新帖。本次新增 3 类需求:

1. **关键词功能**:能按关键词搜索帖子,不拘泥于 CSV 里的指定账号,默认取前 100 帖。
   关键词同时覆盖 **话题标签(hashtag)** 与 **通用关键词(general keyword)** 两种。
2. **单账号参数**:直接传入单个指定账号,无需准备 CSV/txt 文件。
3. **单帖参数**:直接指定单条帖子的链接。

设计目标:把这些都实现为 Stage 1 的**可替换数据源**,复用现有的「下载图 → 入库 →
去重 → 生成评论 → 任务单」下游流程,做到改动面最小、模块边界清晰。

### 关键约束(已与用户确认)

- **优先免登录**:沿用现有 `httpx + x-ig-app-id` 公开接口。免登录是本工具的核心卖点
  (最低封号风险)。
- **instagrapi 兜底**:免登录拿不到数据(返回空 / `login_required`)时,回退到第三方
  `instagrapi`(账号密码 / sessionid 登录)。仅当配置了凭据时才尝试;**绝不自动发评论**,
  兜底只用于「读取」搜索结果。

## 2. CLI 设计

`fetch` 与 `run` 两个子命令新增 4 个**互斥**的数据源参数,外加一个数量参数:

| 参数 | 含义 | 对应需求 |
|---|---|---|
| `--account <handle>` | 单个指定账号(免 CSV/txt) | ② |
| `--post <url>` | 单条帖子链接 | ③ |
| `--hashtag <tag>` | 标签搜索(跨账号) | ①(tag) |
| `--keyword <query>` | 通用关键词搜索(跨账号) | ①(keyword) |
| `--top N` | 标签/关键词取多少帖 | 默认 100 |

规则:
- **不传任何数据源参数 → 沿用现状**(读 `--csv` / `config.yaml` 的 `csv_path`)。
- 四个数据源参数**互斥**:同时传 ≥2 个 → 立即报错退出(argparse 互斥组)。
- `--top` 仅对 `--hashtag` / `--keyword` 生效;默认值来自 `config.yaml` 的 `default_top`(100)。
- `--account` 的取值是账号 handle(支持 `@name`、纯 `name`、或完整 profile URL,经
  现有 `normalize_handle()` 归一化)。
- `--post` 的取值是帖子 URL 或 shortcode。

示例:

```bash
python run.py fetch --account messi
python run.py fetch --post https://www.instagram.com/p/ABC123/
python run.py fetch --hashtag football                 # 默认前 100
python run.py fetch --keyword "world cup" --top 50
python run.py run --hashtag football                   # run 也支持,直接出任务单
python run.py fetch                                     # 不传 → 仍读 CSV(现状)
```

## 3. 抓取策略:两层(免登录主 + instagrapi 兜底)

每种数据源都走「先免登录,失败再 instagrapi」两层。

### 3.1 免登录(主)——`core/ig_fetcher.py`

沿用现有 `httpx(http2=True) + User-Agent + x-ig-app-id` 模式,新增 3 个方法:

- **标签** `search_hashtag(tag, limit) -> list[Post]`
  - 端点:`https://www.instagram.com/api/v1/tags/web_info/?tag_name=<tag>`
  - 解析 `data.top.sections[*]` 与 `data.recent.sections[*]` 下的 medias;每个 media 取
    `code`(→ URL)、`caption.text`、`image_versions2.candidates[0].url`(媒体图)、
    `user.username`(作者)、`taken_at`(时间)、`like_count`、`comment_count`。
  - 去重后按需要取前 `limit` 条(top 优先,recent 补足)。
- **关键词** `search_keyword(query, limit) -> list[Post]`
  - 第一步 topsearch:`https://www.instagram.com/web/search/topsearch/?context=blended&query=<q>`
    → 得到排名的 `hashtags` 与 `users`(topsearch **不直接返回帖子**)。
  - 第二步取帖:优先用最佳匹配标签调用 `search_hashtag`;不足 `limit` 时,再按排名取
    若干热门账号、用现有 `get_recent_posts()` 补帖。
  - 跨来源按 `post_id` 去重,取前 `limit` 条。
- **单帖** `get_single_post(url_or_shortcode) -> Post`
  - 端点:`https://www.instagram.com/p/<shortcode>/embed/captioned/`(免登录拿
    caption + 主图最稳的路子)。解析内嵌 JSON / HTML 得到 caption、主图 URL、作者 handle。
  - 拿不到时间/点赞数也可接受(对写评论非必需)。

`Post` dataclass 新增字段 `owner_handle: str | None = None`:
- `web_profile_info`(单账号/CSV)抓取时,作者 = 被抓账号,保留 `None`,由 `run.py`
  用账号 handle 填充。
- 标签/关键词/单帖抓取时,每帖作者不同,从 payload 填入 `owner_handle`。

### 3.2 instagrapi 兜底——新增 `core/ig_fallback.py`

- 懒加载:仅在免登录返回空 / `login_required`,**且** `.env` 配了凭据
  (`INSTAGRAPI_USERNAME`+`PASSWORD`,或 `INSTAGRAPI_SESSIONID`)时实例化。未配凭据 →
  如实记录「免登录失败且无兜底凭据」并跳过该来源,不报栈、不发评论。
- 复用 `agent/ezcollegeapp-instagram-agent/tools/instagrapi_tool.py` 已验证的登录逻辑
  (稳定 device 指纹、`login_required` 后 relogin 重试一次)。本仓库**自带一份精简实现**,
  不跨目录 import(保持本项目自包含)。
- 方法与免登录层一一对应,返回**同样的 `Post`**(务必带 `owner_handle` 与媒体图 URL):
  - `search_hashtag(tag, limit)` → `hashtag_medias_top/recent`,逐 media 取 `code`、
    `caption_text`、`thumbnail_url`/`resources`(媒体图)、`user.username`、`taken_at`、
    `like_count`。(注:原 `instagrapi_tool.search_hashtag` 未取媒体图 URL,兜底实现需补上。)
  - `search_keyword(query, limit)` → `fbsearch_topsearch` 取标签 → `hashtag_medias_*`。
  - `get_single_post(url)` → `media_pk_from_url` + `media_info`,取 caption + `thumbnail_url`。
- `--account` 因 `web_profile_info` 本身稳定,主走免登录;兜底为可选(`user_id_from_username`
  + `user_medias`),实现时一并提供,行为与其它来源一致。

## 4. 设计取舍(已确认)

- **A. 时间窗口**:`--hashtag` / `--keyword` / `--post` **默认不按 `--hours` 过滤**
  (目标是「前 N 热门/相关帖」);`--account` / CSV **保留**过去 N 小时过滤(盯新帖,现状)。
  若用户显式传 `--hours`,则对标签/关键词结果**额外**叠加该时间窗筛选。
- **B. 关键词解析**:`--keyword` 先 topsearch 得到排名的标签 + 账号,**优先**取最佳标签的帖,
  不足再补热门账号近期帖,跨来源去重后凑够 `--top`。
- **C. 足球语气**:评论提示词是「足球 → 大学申请」定向。关键词搜到非足球内容时类比会牵强
  ——这是工具本意(足球网红引流)。非足球关键词需用户自行改 `prompts/`。**本次不改提示词。**

## 5. 代码结构与改动清单

下游(Stage 2/3)**零改动**——已确认 `comment_generator.build_prompt()` 对缺失的
`account_name`/`account_type`/`country_league` 会回退(如 `account_type` → `"Football account"`),
关键词来的帖子(无 CSV 元数据)能照常生成评论;`store.insert_post()` 接受 `account` dict,
合成一个 `handle = owner_handle`、其余元数据为 NULL 的 dict 即可。

| 文件 | 改动 |
|---|---|
| `core/ig_fetcher.py` | `Post` 加 `owner_handle`;新增 `search_hashtag` / `search_keyword` / `get_single_post`(免登录) |
| `core/ig_fallback.py` | **新增**:instagrapi 兜底封装(自包含),方法与免登录层对应,返回 `Post`(带 `owner_handle` + 媒体图 URL) |
| `run.py` | `fetch`/`run` 加 4 个互斥 source 参数 + `--top`;`cmd_fetch` 加来源选择与兜底编排;把现有「下载图 + 入库」循环抽成 `_ingest_posts(conn, posts, ...)` 复用 |
| `config.yaml` | 新增 `default_top: 100` |
| `.env.example` | 新增可选 `INSTAGRAPI_USERNAME` / `INSTAGRAPI_PASSWORD` / `INSTAGRAPI_SESSIONID` |
| `requirements.txt` | 新增注释掉的 `instagrapi`(与 gpt/gemini 同样按需启用) |
| `README.md` | 文档化新参数与两层抓取策略 |

### 入库与去重

- 标签/关键词/单帖每帖:合成 `account = {"handle": post.owner_handle 或占位, "name": None,
  "type": None, "country_league": None, "followers": None, "url": profile_url}`,调用现有
  `store.insert_post()`。
- 去重不变:`posts.post_id` 主键 + `INSERT OR IGNORE`。跨数据源都不会对同一帖重复评论
  (`tasks.post_id` 主键保证)。已被 CSV 抓过的帖,关键词再次命中也不会重复进任务单。

## 6. 可行性风险

- **免登录标签/关键词端点可能被 IG 限制**:近期 IG 对标签页/搜索收紧,`tags/web_info` 与
  `topsearch` 免登录可能返回空或 `login_required`。设计已用 instagrapi 兜底覆盖此风险;
  实现阶段需实测确认免登录端点当前可用性,并在 README 注明「免登录搜索可能不稳定,
  要稳就配 instagrapi 凭据」。
- **instagrapi 是非官方私有 API**:有封号风险,仅用于读取搜索结果,音量保持低;凭据可选,
  不配则降级为「免登录可用就用,不可用就如实报错」。

## 7. 不做的事(YAGNI)

- 不新增自动发帖/评论(发布仍手动)。
- 不改评论提示词与足球语气。
- 不实现 txt 账号清单解析(需求②是「单账号免文件」,不是「支持 txt」)。
- 不为关键词结果做复杂的相关性重排,沿用 IG 自身排序 + 简单去重。
