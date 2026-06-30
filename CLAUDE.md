# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`auto_reply_influencer` turns a list of football Instagram accounts (or a hashtag / keyword / single post) into a **daily, hand-posted** task list of EZCollegeApp promo comments. The tool **reads** Instagram and writes comments with an LLM; it **never auto-posts** — publishing and screenshotting are done manually by a human (lowest ban risk). The comment style uses a "football → college application" analogy to drop the EZCollegeApp brand name naturally (brand mention only, **no links**).

The README is in Chinese and is the user-facing manual; this file is the engineering map.

## Commands

```bash
# Setup
pip install -r requirements.txt        # only Stage-1 deps are required; LLM/instagrapi deps are optional & commented
cp .env.example .env                    # IG_PROXY optional (blank = direct local-IP fetch); LLM/instagrapi keys optional

# Full pipeline (fetch -> generate -> tasks)
python run.py run --hours 2

# Individual stages
python run.py fetch --hours 2 [--limit N] [--csv path]   # Stage 1 (default source = CSV)
python run.py generate [--provider gpt] [--limit N]       # Stage 2
python run.py tasks [--max-tasks-per-day N]               # Stage 3

# Alternate Stage-1 sources (mutually exclusive; no flag = CSV). On fetch AND run:
python run.py fetch --account <handle>                    # single account, no CSV file
python run.py fetch --post <url-or-shortcode>             # single post
python run.py fetch --hashtag <tag>  [--top 100]          # hashtag search across accounts
python run.py fetch --keyword "<query>" [--top 100]       # general keyword search across accounts

python run.py mark-done --post-id <id> --outcome survived|hidden|removed --screenshot <path>
python run.py stats

# Tests — MUST run from the repo root (root conftest.py puts the repo on sys.path so `import core`/`import run` resolve)
python -m pytest -q
python -m pytest tests/test_run_helpers.py::test_select_source_defaults_to_csv -v   # single test
```

There is no build step, linter config, or CI in this repo. `python -m pytest` is the only check.

## Architecture

Three stages, orchestrated by `run.py`, sharing state in **SQLite** (`data/influencer.db`). All runtime output (`data/`, `media/`, `daily_tasks/`) is gitignored and regenerated.

```
source ──▶ ① fetch ──▶ posts table + media/  ──▶ ② generate ──▶ tasks table ──▶ ③ tasks ──▶ daily_tasks/<date>/
(CSV/account/                (dedup by post_id)     (LLM + guardrails)  (one task per post)   (human posts by hand)
 post/hashtag/keyword)
```

- **Stage 1 — sourcing (`core/ig_fetcher.py`, `core/ig_fallback.py`, `run.py:cmd_fetch`).** `run.py` picks the source (`select_source`) and routes to `_cmd_fetch_accounts` (CSV / `--account`) or `_cmd_fetch_search` (`--post`/`--hashtag`/`--keyword`). Each source tries **login-free first** then **instagrapi fallback**:
  - `core/ig_fetcher.py` is the login-free reader (Instagram public web endpoints, **no login**): `get_recent_posts` (per account via `web_profile_info`), `search_hashtag` (`tags/web_info`), `search_keyword` (`topsearch` → resolve to hashtags + accounts → fetch), `get_single_post` (the `/p/<code>/embed/` page). All return the normalized `Post` dataclass.
  - `core/ig_fallback.py` wraps the third-party **instagrapi** (account login) as a **reads-only** fallback, used by `_gather_with_fallback` only when login-free returns nothing **and** credentials are set (`fallback_available()`). `instagrapi` is imported lazily, so the module loads without it installed.
  - `core/store.py` does the dedup + media download + insert. `_ingest_posts` (in `run.py`) downloads each image and `INSERT OR IGNORE`s the post.

- **Stage 2 — comment generation (`core/comment_generator.py` + `core/llm_client.py`).** `build_prompt` fills `prompts/comment_prompt.md` (rules) + `prompts/examples.md` (few-shot) with the post's metadata, then `LLMClient.analyze_post` sends prompt **+ the post image** to a vision model and parses a JSON object (`comment` + self-check fields). Hard **guardrails live in code** (`validate_comment`), not the prompt: reject links, over-length, missing "EZCollegeApp" mention, too many hashtags. On violation it regenerates **once**, then marks the post `skipped` (so it's never retried forever).

- **Stage 3 — task package (`core/task_writer.py`).** Writes `daily_tasks/<date>/` (a readable `tasks.md`, `tasks.json`, and a per-post folder with image + `comment.txt` + a `_README.txt` containing the exact `mark-done` command). Idempotent: a post is written at most once (`tasks.task_date` stamp); re-running the same day appends only new tasks.

## Invariants and gotchas (don't "simplify" these away)

- **No auto-posting, ever.** Reading is low-risk; posting is the high-risk action and stays manual. The instagrapi fallback is reads-only — do not add write/comment/follow calls to the pipeline.
- **HTTP/2 is mandatory** for the IG endpoints in `ig_fetcher.py` (`httpx` with `http2=True` + the `x-ig-app-id` header). IG returns 429 to HTTP/1.1 here, so never swap in `requests`/`urllib`. A fresh `httpx.Client` per request is intentional — with a rotating `IG_PROXY` gateway it yields a new exit IP per call.
- **Login-free is the whole point** (lowest ban risk). Keep it the primary path; instagrapi is a credential-gated fallback only.
- **No links in comments** (`config.yaml: allow_link: false`). Links are the top trigger for comment removal / account flagging — brand mention only.
- **Dedup is structural, not best-effort:** `posts.post_id` PK = a post is never re-fetched; `tasks.post_id` PK = a post becomes a comment task at most once. This holds across all sources.
- **Time-window semantics differ by source:** account/CSV keep only posts from the last `--hours` (default `time_window_hours`, 2); `--hashtag`/`--keyword`/`--post` take the top N with **no** time filter unless `--hours` is explicitly passed.
- **Keyword/hashtag posts carry no CSV metadata** (`account_type` etc. are blank). `build_prompt` falls back `account_type` → `"Football account"`. The prompt is football→college-themed, so non-football keywords yield strained analogies — adjust `prompts/` for other niches.
- **LLM provider default is `claude-cli`** (shells out to the `claude` binary, no API key; auto-falls back to `claude-api` if the binary is absent). `gpt`/`gemini`/`claude-api` lazy-import their SDKs, so the default path needs none of them.
- **Config precedence:** CLI flags > `config.yaml` > `_DEFAULTS` in `run.py` (keep `_DEFAULTS` and `config.yaml` in sync when adding a key).
- **Tuning without code:** comment voice → `prompts/comment_prompt.md` + `prompts/examples.md`; parameters (window, caps, provider, `default_top`) → `config.yaml`.

## Testing approach

Tests cover the **pure** logic: payload parsers (`_parse_hashtag_posts`, `_parse_topsearch`, `_parse_embed_post`, `_media_to_post`) against realistic IG response fixtures, and orchestration helpers (`select_source`, `_ingest_posts`, `_gather_with_fallback`) with monkeypatched I/O and a real in-memory SQLite. **Network calls and live instagrapi calls are intentionally not unit-tested** — when adding a feature, factor the parse/decision logic into a pure function and test that, rather than mocking httpx end-to-end.
