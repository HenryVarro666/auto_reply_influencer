"""SQLite storage + media download — the dedup and state layer.

Two tables:

  posts  — one row per fetched Instagram post. UNIQUE on post_id, so re-running
           the fetch never re-saves a post (the "do not duplicate" guarantee for
           Stage 1). Only on a genuinely new row do we download the image.

  tasks  — one row per post we have generated a comment for. UNIQUE on post_id,
           so a post becomes a task at most once (the "do not reply to the same
           post twice" guarantee for Stage 3). status/outcome/screenshot_path
           record the manual-post result for the experiment feedback loop.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from pathlib import Path

import httpx

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id           TEXT PRIMARY KEY,
    handle            TEXT NOT NULL,
    account_name      TEXT,
    account_type      TEXT,
    country_league    TEXT,
    followers         INTEGER,
    url               TEXT,
    caption           TEXT,
    media_url         TEXT,
    media_path        TEXT,
    posted_at         TEXT,
    like_count        INTEGER,
    comment_count     INTEGER,
    comments_disabled INTEGER,
    fetched_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    post_id         TEXT PRIMARY KEY REFERENCES posts(post_id),
    comment         TEXT,
    provider        TEXT,
    metaphor        TEXT,
    reason          TEXT,             -- why skipped, if status='skipped'
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | done | skipped
    outcome         TEXT,             -- survived | hidden | removed
    screenshot_path TEXT,
    task_date       TEXT,             -- date written into a daily package (NULL = not yet)
    generated_at    TEXT NOT NULL,
    updated_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, task_date);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def today_str() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def connect(db_path: str | os.PathLike) -> sqlite3.Connection:
    """Open (and initialize) the SQLite database."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


# --- Stage 1: posts ---------------------------------------------------------
def post_exists(conn: sqlite3.Connection, post_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,))
    return cur.fetchone() is not None


def insert_post(conn: sqlite3.Connection, post, account: dict, media_path: str | None) -> bool:
    """INSERT OR IGNORE a post. Returns True only if a new row was written."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO posts (
            post_id, handle, account_name, account_type, country_league,
            followers, url, caption, media_url, media_path, posted_at,
            like_count, comment_count, comments_disabled, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            post.post_id,
            account["handle"],
            account.get("name"),
            account.get("type"),
            account.get("country_league"),
            account.get("followers"),
            post.url,
            post.caption,
            post.media_url,
            media_path,
            post.posted_at,
            post.like_count,
            post.comment_count,
            1 if post.comments_disabled else 0,
            _now(),
        ),
    )
    conn.commit()
    return cur.rowcount > 0


def download_media(media_url: str, dest: str | os.PathLike, *, proxy: str | None = None,
                   timeout: float = 30.0) -> bool:
    """Download a post image to ``dest``.

    Tries a direct connection first (CDN images are public and geo-agnostic, so
    we save proxy bandwidth); falls back to the proxy if the direct attempt
    fails. Returns True on success.
    """
    if not media_url:
        return False
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0"}
    attempts = [None]
    if proxy:
        attempts.append(proxy)
    for px in attempts:
        try:
            kwargs: dict = {"timeout": timeout, "follow_redirects": True, "headers": headers}
            if px:
                kwargs["proxy"] = px
            with httpx.Client(**kwargs) as client:
                resp = client.get(media_url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return True
        except Exception:
            continue
    return False


# --- Stage 2: comment generation -------------------------------------------
def posts_needing_comment(conn: sqlite3.Connection, limit: int | None = None) -> list[sqlite3.Row]:
    """Posts that have no task row yet (and have a downloaded image to read)."""
    sql = """
        SELECT p.* FROM posts p
        LEFT JOIN tasks t ON t.post_id = p.post_id
        WHERE t.post_id IS NULL
          AND p.media_path IS NOT NULL
        ORDER BY p.fetched_at DESC
    """
    if limit:
        sql += " LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()
    return conn.execute(sql).fetchall()


def save_comment(conn: sqlite3.Connection, post_id: str, *, comment: str | None,
                 provider: str, metaphor: str | None = None, status: str = "pending",
                 reason: str | None = None) -> None:
    """Create the task row for a post (INSERT OR IGNORE — never overwrites)."""
    conn.execute(
        """
        INSERT OR IGNORE INTO tasks (
            post_id, comment, provider, metaphor, reason, status,
            generated_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (post_id, comment, provider, metaphor, reason, status, _now(), _now()),
    )
    conn.commit()


# --- Stage 3: daily tasks ---------------------------------------------------
def tasks_for_today(conn: sqlite3.Connection, max_count: int) -> list[sqlite3.Row]:
    """Pending tasks not yet placed into any daily package, joined with post data.

    Capped at ``max_count`` (the per-day comment budget).
    """
    return conn.execute(
        """
        SELECT t.*, p.handle, p.account_name, p.account_type, p.country_league,
               p.url AS post_url, p.caption, p.media_path, p.posted_at
        FROM tasks t JOIN posts p ON p.post_id = t.post_id
        WHERE t.status = 'pending' AND t.task_date IS NULL AND t.comment IS NOT NULL
        ORDER BY t.generated_at ASC
        LIMIT ?
        """,
        (max_count,),
    ).fetchall()


def mark_task_written(conn: sqlite3.Connection, post_id: str, task_date: str) -> None:
    conn.execute(
        "UPDATE tasks SET task_date = ?, updated_at = ? WHERE post_id = ?",
        (task_date, _now(), post_id),
    )
    conn.commit()


def mark_done(conn: sqlite3.Connection, post_id: str, *, outcome: str | None = None,
              screenshot_path: str | None = None, status: str = "done") -> bool:
    """Record the manual-post result. Returns True if a task row was updated."""
    cur = conn.execute(
        """
        UPDATE tasks
        SET status = ?, outcome = COALESCE(?, outcome),
            screenshot_path = COALESCE(?, screenshot_path), updated_at = ?
        WHERE post_id = ?
        """,
        (status, outcome, screenshot_path, _now(), post_id),
    )
    conn.commit()
    return cur.rowcount > 0


def stats(conn: sqlite3.Connection) -> dict:
    """Quick counts for CLI output / experiment tracking."""
    def scalar(sql: str, *args) -> int:
        return conn.execute(sql, args).fetchone()[0]

    return {
        "posts": scalar("SELECT COUNT(*) FROM posts"),
        "tasks_pending": scalar("SELECT COUNT(*) FROM tasks WHERE status='pending'"),
        "tasks_skipped": scalar("SELECT COUNT(*) FROM tasks WHERE status='skipped'"),
        "tasks_done": scalar("SELECT COUNT(*) FROM tasks WHERE status='done'"),
        "outcome_survived": scalar("SELECT COUNT(*) FROM tasks WHERE outcome='survived'"),
        "outcome_hidden": scalar("SELECT COUNT(*) FROM tasks WHERE outcome='hidden'"),
        "outcome_removed": scalar("SELECT COUNT(*) FROM tasks WHERE outcome='removed'"),
    }
