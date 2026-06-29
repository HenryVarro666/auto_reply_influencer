"""Daily task package writer — Stage 3.

Writes ``daily_tasks/<date>/`` so a human can post the comments by hand:

  daily_tasks/<date>/
    tasks.md                 # readable checklist
    tasks.json               # machine-readable
    <post_id>/
      image.jpg              # the post image (copied)
      comment.txt            # the exact text to paste
      _README.txt            # where to drop your screenshot + the mark-done cmd

A post is written at most once (its task row gets ``task_date`` stamped), and
re-running the same day appends only newly-generated tasks instead of clobbering
the file.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import store


def _excerpt(text: str | None, n: int = 200) -> str:
    text = (text or "").strip().replace("\r", " ")
    text = " ".join(text.split("\n"))
    return text if len(text) <= n else text[:n] + "…"


def _task_to_dict(row, image_rel: str) -> dict:
    return {
        "post_id": row["post_id"],
        "handle": row["handle"],
        "account_name": row["account_name"],
        "account_type": row["account_type"],
        "country_league": row["country_league"],
        "post_url": row["post_url"],
        "posted_at": row["posted_at"],
        "image": image_rel,
        "caption": row["caption"],
        "comment": row["comment"],
        "metaphor": row["metaphor"],
    }


def _render_md(date: str, tasks: list[dict]) -> str:
    lines = [
        f"# Daily comment tasks — {date}",
        "",
        f"{len(tasks)} comment(s) to post by hand. Open each post, paste the comment, "
        "screenshot it, then record the result.",
        "",
    ]
    for i, t in enumerate(tasks, 1):
        lines += [
            f"## {i}. @{t['handle']} — {t['account_name']} ({t['account_type']})",
            f"- Post: {t['post_url']}",
            f"- Posted: {t['posted_at']}",
            f"- Image: `{t['image']}`",
            f"- Caption: {_excerpt(t['caption'])}",
            "",
            "**Comment to post:**",
            "",
            f"> {t['comment']}",
            "",
            f"- [ ] Posted. Then run: `python run.py mark-done --post-id {t['post_id']} "
            "--outcome survived --screenshot "
            f"daily_tasks/{date}/{t['post_id']}/screenshot.png`",
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


def write_daily_tasks(conn, tasks_dir: str | os.PathLike, max_tasks_per_day: int) -> dict:
    """Materialize today's task package. Returns a summary dict."""
    date = store.today_str()
    day_dir = Path(tasks_dir) / date
    day_dir.mkdir(parents=True, exist_ok=True)
    json_path = day_dir / "tasks.json"

    # Merge with anything already written today (supports multiple runs/day).
    existing: list[dict] = []
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    already = {t["post_id"] for t in existing}

    new_rows = store.tasks_for_today(conn, max_tasks_per_day)
    added: list[dict] = []
    for row in new_rows:
        if row["post_id"] in already:
            continue
        post_dir = day_dir / str(row["post_id"])
        post_dir.mkdir(parents=True, exist_ok=True)

        # Copy the image into the task folder.
        image_rel = ""
        src = row["media_path"]
        if src and Path(src).exists():
            ext = Path(src).suffix or ".jpg"
            dst = post_dir / f"image{ext}"
            shutil.copyfile(src, dst)
            image_rel = os.path.relpath(dst, Path.cwd())

        (post_dir / "comment.txt").write_text(row["comment"] or "", encoding="utf-8")
        (post_dir / "_README.txt").write_text(
            "1. Open the post: " + (row["post_url"] or "") + "\n"
            "2. Paste the text from comment.txt as a comment.\n"
            "3. Screenshot your posted comment and save it here as screenshot.png\n"
            "4. Record the result:\n"
            f"   python run.py mark-done --post-id {row['post_id']} "
            "--outcome survived --screenshot "
            f"daily_tasks/{date}/{row['post_id']}/screenshot.png\n"
            "   (outcome: survived | hidden | removed)\n",
            encoding="utf-8",
        )

        added.append(_task_to_dict(row, image_rel))
        store.mark_task_written(conn, row["post_id"], date)

    all_tasks = existing + added
    json_path.write_text(json.dumps(all_tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    (day_dir / "tasks.md").write_text(_render_md(date, all_tasks), encoding="utf-8")

    return {"date": date, "added": len(added), "total_today": len(all_tasks),
            "dir": str(day_dir)}
