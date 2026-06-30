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
from pathlib import Path

import yaml
from dotenv import load_dotenv

from core import comment_generator, store, task_writer
from core.ig_fetcher import FetchError, InstagramFetcher, ProfileNotFound, normalize_handle
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


# --- Stage 1 ----------------------------------------------------------------
def cmd_fetch(args, cfg) -> None:
    hours = args.hours if args.hours is not None else cfg["time_window_hours"]
    csv_path = _abs(args.csv or cfg["csv_path"])
    media_dir = _abs(cfg["media_dir"])
    conn = store.connect(_abs(cfg["db_path"]))
    proxy = os.getenv("IG_PROXY") or None
    if not proxy:
        print("ℹ️  未设置 IG_PROXY，默认使用本地 IP 直连。"
              "（账号较多时本地 IP 可能被限流；要稳可在 .env 配置代理。）")

    fetcher = InstagramFetcher(proxy, retries=int(cfg["fetch_retries"]))
    accounts = read_accounts(csv_path, args.limit)
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    date = store.today_str()

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
            recent = [p for p in posts if _within_window(p.posted_at, cutoff)]
            fresh = 0
            for p in recent:
                if store.post_exists(conn, p.post_id):
                    existing += 1
                    continue
                media_path = media_dir / date / handle / f"{p.post_id}.jpg"
                ok = store.download_media(p.media_url, media_path, proxy=proxy)
                store.insert_post(conn, p, acc, str(media_path) if ok else None)
                new_posts += 1
                fresh += 1
            if fresh:
                _emit(bar, f"  ✓ @{handle}: 窗口内 {len(recent)} 帖，新增 {fresh}")
        _advance(bar, f"@{handle} 新{new_posts} 旧{existing} 错{errors}")
        if i < len(accounts):
            time.sleep(random.uniform(float(cfg["min_delay_seconds"]),
                                      float(cfg["max_delay_seconds"])))
    _close(bar)
    print(f"✅ 抓帖完成：新增 {new_posts} 帖，已存在 {existing}，账号错误 {errors}。")


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Instagram influencer auto-comment pipeline.")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="Stage 1: fetch recent posts")
    f.add_argument("--hours", type=float, default=None, help="time window (default from config)")
    f.add_argument("--limit", type=int, default=None, help="only first N accounts")
    f.add_argument("--csv", default=None, help="override CSV path")
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
