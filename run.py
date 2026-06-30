#!/usr/bin/env python3
"""auto_reply_influencer — CLI orchestrator.

Subcommands:
  fetch      Stage 1 — pull recent posts (last N hours) for CSV accounts, dedup, save.
  generate   Stage 2 — LLM reads each new post and writes a promo comment.
  tasks      Stage 3 — write today's daily_tasks/<date>/ package to post by hand.
  run        fetch -> generate -> tasks in one go.
  mark-done  record the result after you post a comment manually.
  stats      print pipeline + experiment counters.

Run `python run.py <subcommand> -h` for flags.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv

from core import comment_generator, ig_fallback, store, task_writer
from core.ig_fetcher import FetchError, InstagramFetcher, Post, ProfileNotFound, normalize_handle
from core.llm_client import LLMClient, LLMError

try:
    from tqdm import tqdm as _tqdm
except ImportError:  # graceful fallback to plain prints
    _tqdm = None

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


# --- progress bar helpers (no-op when tqdm is unavailable) ------------------
def _make_bar(total: int, desc: str, unit: str):
    if _tqdm is None:
        return None
    return _tqdm(total=total, desc=desc, unit=unit, dynamic_ncols=True, leave=True)


def _emit(bar, msg: str) -> None:
    """Print a line without breaking the progress bar."""
    if bar is not None:
        bar.write(msg)
    else:
        print(msg)


def _advance(bar, postfix: str | None = None) -> None:
    if bar is not None:
        if postfix:
            bar.set_postfix_str(postfix)
        bar.update(1)


def _close(bar) -> None:
    if bar is not None:
        bar.close()

_DEFAULTS = {
    "csv_path": "instagram_football_top100_formatted.csv",
    "time_window_hours": 2,
    "posts_per_account": 12,
    "fetch_retries": 5,
    "min_delay_seconds": 1.0,
    "max_delay_seconds": 3.0,
    "provider": "claude-cli",
    "max_comment_chars": 160,
    "max_hashtags": 2,
    "allow_link": False,
    "max_tasks_per_day": 20,
    "db_path": "data/influencer.db",
    "media_dir": "media",
    "tasks_dir": "daily_tasks",
    "default_top": 100,
}


def load_config() -> dict:
    cfg = dict(_DEFAULTS)
    path = ROOT / "config.yaml"
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cfg.update({k: v for k, v in loaded.items() if v is not None})
    return cfg


def _abs(cfg_value: str) -> Path:
    p = Path(cfg_value)
    return p if p.is_absolute() else ROOT / p


@dataclass
class Source:
    kind: str                 # csv | account | post | hashtag | keyword
    value: str | None = None  # handle / url / tag / query


def select_source(args) -> Source:
    """Map CLI args to a Source. Mutual exclusivity is enforced by argparse;
    if no source flag is set we default to the CSV account list."""
    if getattr(args, "account", None):
        return Source("account", args.account)
    if getattr(args, "post", None):
        return Source("post", args.post)
    if getattr(args, "hashtag", None):
        return Source("hashtag", args.hashtag)
    if getattr(args, "keyword", None):
        return Source("keyword", args.keyword)
    return Source("csv")


def read_accounts(csv_path: Path, limit: int | None = None) -> list[dict]:
    """Parse the influencer CSV into account dicts (with a normalized handle)."""
    accounts: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            url = (row.get("Instagram URL") or "").strip()
            handle = normalize_handle(url)
            if not handle:
                continue
            followers = (row.get("Followers") or "").strip()
            accounts.append({
                "handle": handle,
                "name": (row.get("Name") or "").strip(),
                "type": (row.get("Type") or "").strip(),
                "country_league": (row.get("Country / League") or "").strip(),
                "followers": int(followers) if followers.isdigit() else None,
                "url": url,
            })
            if limit and len(accounts) >= limit:
                break
    return accounts


def _within_window(posted_at: str | None, cutoff: _dt.datetime) -> bool:
    if not posted_at:
        return False
    try:
        dt = _dt.datetime.fromisoformat(posted_at)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt >= cutoff


def _account_for_post(post: Post) -> dict:
    """Synthesize an account dict for a post whose owner came from search results."""
    handle = post.owner_handle or "unknown"
    url = f"https://www.instagram.com/{handle}/" if handle != "unknown" else ""
    return {"handle": handle, "name": "", "type": "", "country_league": "",
            "followers": None, "url": url}


def _ingest_posts(conn, items, *, media_dir, date, proxy, cutoff) -> tuple[int, int]:
    """Download + insert each (Post, account) pair. Returns (new, existing).

    cutoff=None means no time filter (hashtag/keyword/post); a datetime applies
    the 'published within the window' filter (account/CSV).
    """
    new = existing = 0
    for post, account in items:
        if cutoff is not None and not _within_window(post.posted_at, cutoff):
            continue
        if store.post_exists(conn, post.post_id):
            existing += 1
            continue
        handle = account["handle"]
        media_path = media_dir / date / handle / f"{post.post_id}.jpg"
        ok = store.download_media(post.media_url, media_path, proxy=proxy)
        store.insert_post(conn, post, account, str(media_path) if ok else None)
        new += 1
    return new, existing


def _gather_with_fallback(primary: Callable[[], list], fallback, *, label: str) -> list:
    """Run primary(); on error or empty result fall back to fallback() if provided.

    primary/fallback are zero-arg callables returning list[Post]; fallback may be None.
    """
    try:
        posts = primary() or []
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ {label}: 免登录失败 — {exc}")
        posts = []
    if posts:
        return posts
    if fallback is None:
        print(f"  ℹ {label}: 免登录无结果，且未配置 instagrapi 兜底凭据，跳过。")
        return []
    print(f"  ↪ {label}: 免登录无结果，改用 instagrapi 兜底…")
    try:
        return fallback() or []
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {label}: instagrapi 兜底也失败 — {exc}")
        return []


# --- Stage 1 ----------------------------------------------------------------
def cmd_fetch(args, cfg) -> None:
    proxy = os.getenv("IG_PROXY") or None
    if not proxy:
        print("ℹ️  未设置 IG_PROXY，默认使用本地 IP 直连。"
              "（账号较多时本地 IP 可能被限流；要稳可在 .env 配置代理。）")
    media_dir = _abs(cfg["media_dir"])
    conn = store.connect(_abs(cfg["db_path"]))
    fetcher = InstagramFetcher(proxy, retries=int(cfg["fetch_retries"]))
    date = store.today_str()
    source = select_source(args)

    if source.kind in ("csv", "account"):
        _cmd_fetch_accounts(args, cfg, conn, fetcher, proxy, media_dir, date, source)
    else:
        _cmd_fetch_search(args, cfg, conn, fetcher, proxy, media_dir, date, source)


def _cmd_fetch_accounts(args, cfg, conn, fetcher, proxy, media_dir, date, source) -> None:
    hours = args.hours if args.hours is not None else cfg["time_window_hours"]
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)

    if source.kind == "account":
        handle = normalize_handle(source.value)
        if not handle:
            print(f"无法解析账号：{source.value!r}")
            return
        accounts = [{"handle": handle, "name": "", "type": "", "country_league": "",
                     "followers": None, "url": f"https://www.instagram.com/{handle}/"}]
    else:
        accounts = read_accounts(_abs(args.csv or cfg["csv_path"]), args.limit)

    new_posts = existing = errors = 0
    print(f"① 抓帖：{len(accounts)} 个账号 · 窗口=过去 {hours}h · "
          f"出口={'代理 IP' if proxy else '本地 IP 直连'}")
    bar = _make_bar(len(accounts), "① 抓帖", "账号")
    for i, acc in enumerate(accounts, 1):
        handle = acc["handle"]
        posts = None
        try:
            posts = fetcher.get_recent_posts(handle, limit=int(cfg["posts_per_account"]))
        except ProfileNotFound:
            errors += 1
            _emit(bar, f"  ✗ @{handle}: 账号不存在 (404)")
        except FetchError as exc:
            errors += 1
            _emit(bar, f"  ✗ @{handle}: 抓取失败 — {exc}")

        if posts is not None:
            n, e = _ingest_posts(conn, [(p, acc) for p in posts], media_dir=media_dir,
                                 date=date, proxy=proxy, cutoff=cutoff)
            new_posts += n
            existing += e
            if n:
                _emit(bar, f"  ✓ @{handle}: 新增 {n}")
        _advance(bar, f"@{handle} 新{new_posts} 旧{existing} 错{errors}")
        if i < len(accounts):
            time.sleep(random.uniform(float(cfg["min_delay_seconds"]),
                                      float(cfg["max_delay_seconds"])))
    _close(bar)
    print(f"✅ 抓帖完成：新增 {new_posts} 帖，已存在 {existing}，账号错误 {errors}。")


def _cmd_fetch_search(args, cfg, conn, fetcher, proxy, media_dir, date, source) -> None:
    top = args.top if getattr(args, "top", None) else int(cfg["default_top"])
    cutoff = None
    if args.hours is not None:
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=args.hours)
    fb = ig_fallback.InstagrapiFallback() if ig_fallback.fallback_available() else None

    if source.kind == "hashtag":
        label = f"标签 #{source.value.lstrip('#')}"
        primary = lambda: fetcher.search_hashtag(source.value, top)
        fallback = (lambda: fb.search_hashtag(source.value, top)) if fb else None
    elif source.kind == "keyword":
        label = f"关键词 “{source.value}”"
        primary = lambda: fetcher.search_keyword(source.value, top)
        fallback = (lambda: fb.search_keyword(source.value, top)) if fb else None
    else:  # post
        label = f"单帖 {source.value}"
        primary = lambda: [fetcher.get_single_post(source.value)]
        fallback = (lambda: [p for p in [fb.get_single_post(source.value)] if p]) if fb else None

    window_note = "" if cutoff is None else f" · 仅过去 {args.hours}h"
    print(f"① 抓帖（{label}）· 取前 {top} · "
          f"出口={'代理 IP' if proxy else '本地 IP 直连'}{window_note}")
    posts = _gather_with_fallback(primary, fallback, label=label)
    items = [(p, _account_for_post(p)) for p in posts]
    new, existing = _ingest_posts(conn, items, media_dir=media_dir, date=date,
                                  proxy=proxy, cutoff=cutoff)
    print(f"✅ 抓帖完成：{label} 命中 {len(posts)} 帖，新增 {new}，已存在 {existing}。")


# --- Stage 2 ----------------------------------------------------------------
def cmd_generate(args, cfg) -> None:
    provider = args.provider or cfg["provider"]
    conn = store.connect(_abs(cfg["db_path"]))
    try:
        llm = LLMClient(provider)
    except LLMError as exc:
        print(f"LLM init failed: {exc}", file=sys.stderr)
        sys.exit(1)

    posts = store.posts_needing_comment(conn, args.limit)
    if not posts:
        print("没有需要生成评论的帖子。先运行 `fetch`？")
        return
    speed = "约 ~45 秒/条" if llm.provider.startswith("claude") else "约 ~3 秒/条"
    print(f"② 生成评论：{len(posts)} 条待处理 · 模型={llm.provider}（{speed}）")
    ok = skipped = 0
    bar = _make_bar(len(posts), "② 写评论", "帖")
    for row in posts:
        post = dict(row)
        res = comment_generator.generate(
            llm, post,
            max_chars=int(cfg["max_comment_chars"]),
            max_hashtags=int(cfg["max_hashtags"]),
            allow_link=bool(cfg["allow_link"]),
        )
        store.save_comment(conn, post["post_id"], comment=res.comment, provider=llm.provider,
                           metaphor=res.metaphor, status=res.status, reason=res.reason)
        if res.status == "pending":
            ok += 1
            _emit(bar, f"  ✓ @{post['handle']}: {res.comment}")
        else:
            skipped += 1
            _emit(bar, f"  ✗ @{post['handle']}: 跳过 ({res.reason})")
        _advance(bar, f"成功{ok} 跳过{skipped}")
    _close(bar)
    print(f"✅ 生成 {ok} 条评论，跳过 {skipped}。")


# --- Stage 3 ----------------------------------------------------------------
def cmd_tasks(args, cfg) -> None:
    conn = store.connect(_abs(cfg["db_path"]))
    cap = args.max_tasks_per_day if args.max_tasks_per_day is not None else cfg["max_tasks_per_day"]
    summary = task_writer.write_daily_tasks(conn, _abs(cfg["tasks_dir"]), int(cap))
    print(f"③ 任务单：新增 {summary['added']} 条（今日共 {summary['total_today']} 条）→ "
          f"{summary['dir']}")
    if summary["total_today"]:
        print(f"   打开 {summary['dir']}/tasks.md 即可照着手动发布。")


def cmd_run(args, cfg) -> None:
    cmd_fetch(args, cfg)
    cmd_generate(args, cfg)
    cmd_tasks(args, cfg)


def cmd_mark_done(args, cfg) -> None:
    conn = store.connect(_abs(cfg["db_path"]))
    ok = store.mark_done(conn, args.post_id, outcome=args.outcome,
                         screenshot_path=args.screenshot, status=args.status)
    print(f"{'Updated' if ok else 'No task found for'} post {args.post_id}.")


def cmd_stats(args, cfg) -> None:
    conn = store.connect(_abs(cfg["db_path"]))
    for k, v in store.stats(conn).items():
        print(f"  {k:18s} {v}")


def _add_source_args(parser) -> None:
    """Attach the mutually-exclusive Stage-1 source flags + --top to a subparser."""
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--account", default=None,
                     help="single account handle/URL (no CSV needed)")
    grp.add_argument("--post", default=None,
                     help="single post URL or shortcode")
    grp.add_argument("--hashtag", default=None,
                     help="search a hashtag across accounts (default top 100)")
    grp.add_argument("--keyword", default=None,
                     help="general keyword search across accounts (default top 100)")
    parser.add_argument("--top", type=int, default=None,
                        help="max posts for hashtag/keyword search (default config.default_top=100)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Instagram influencer auto-comment pipeline.")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Stage 1: fetch recent posts")
    f.add_argument("--hours", type=float, default=None, help="time window (default from config)")
    f.add_argument("--limit", type=int, default=None, help="only first N accounts (CSV source)")
    f.add_argument("--csv", default=None, help="override CSV path")
    _add_source_args(f)
    f.set_defaults(func=cmd_fetch)

    g = sub.add_parser("generate", help="Stage 2: generate comments")
    g.add_argument("--provider", default=None, help="claude-cli | claude-api | gpt | gemini")
    g.add_argument("--limit", type=int, default=None, help="only N posts")
    g.set_defaults(func=cmd_generate)

    t = sub.add_parser("tasks", help="Stage 3: write daily task package")
    t.add_argument("--max-tasks-per-day", type=int, default=None, dest="max_tasks_per_day")
    t.set_defaults(func=cmd_tasks)

    r = sub.add_parser("run", help="fetch + generate + tasks")
    r.add_argument("--hours", type=float, default=None)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--csv", default=None)
    r.add_argument("--provider", default=None)
    r.add_argument("--max-tasks-per-day", type=int, default=None, dest="max_tasks_per_day")
    _add_source_args(r)
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("mark-done", help="record a manual-post result")
    m.add_argument("--post-id", required=True)
    m.add_argument("--outcome", choices=["survived", "hidden", "removed"], default=None)
    m.add_argument("--screenshot", default=None)
    m.add_argument("--status", choices=["done", "skipped", "pending"], default="done")
    m.set_defaults(func=cmd_mark_done)

    s = sub.add_parser("stats", help="show counters")
    s.set_defaults(func=cmd_stats)
    return p


def main() -> None:
    cfg = load_config()
    args = build_parser().parse_args()
    args.func(args, cfg)


if __name__ == "__main__":
    main()
