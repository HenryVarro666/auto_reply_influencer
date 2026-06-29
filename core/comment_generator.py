"""Comment generation + guardrails — Stage 2 logic.

Assembles the prompt from ``prompts/comment_prompt.md`` + ``prompts/examples.md``
and the post's metadata, calls the LLM (which also reads the post image), then
enforces hard guardrails the LLM cannot talk its way around (no link, length,
brand mention, hashtag cap). On failure it regenerates once, then gives up and
marks the post 'skipped' so it is never retried forever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .llm_client import LLMClient, LLMError

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# A loose URL / "link in bio" detector for the no-link rule.
_URL_RE = re.compile(r"(https?://|www\.|\b\S+\.(com|net|org|io|app|co|me|gg)\b|link in bio)",
                     re.IGNORECASE)
_HASHTAG_RE = re.compile(r"#\w+")


@dataclass
class GenResult:
    comment: str | None
    metaphor: str | None
    status: str          # 'pending' (good) | 'skipped'
    reason: str | None   # why skipped / last validation error


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding="utf-8")


def build_prompt(post: dict, *, max_chars: int, max_hashtags: int) -> str:
    template = _load("comment_prompt.md")
    examples = _load("examples.md")
    image_abs = str(Path(post["media_path"]).resolve())
    caption = (post["caption"] or "").strip() or "(no caption)"
    repl = {
        "{NAME}": post["account_name"] or post["handle"],
        "{TYPE}": post["account_type"] or "Football account",
        "{COUNTRY_LEAGUE}": post["country_league"] or "",
        "{CAPTION}": caption[:600],
        "{IMAGE_PATH}": image_abs,
        "{MAX_CHARS}": str(max_chars),
        "{MAX_HASHTAGS}": str(max_hashtags),
        "{EXAMPLES}": examples,
    }
    out = template
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def validate_comment(comment: str, *, max_chars: int, max_hashtags: int,
                     allow_link: bool) -> list[str]:
    """Return a list of guardrail violations (empty = OK)."""
    errors: list[str] = []
    if not comment or not comment.strip():
        return ["empty comment"]
    text = comment.strip()
    if len(text) > max_chars:
        errors.append(f"too long ({len(text)} > {max_chars} chars)")
    if not allow_link and _URL_RE.search(text):
        errors.append("contains a link/URL (not allowed)")
    if "ezcollegeapp" not in text.lower():
        errors.append("does not mention EZCollegeApp")
    if len(_HASHTAG_RE.findall(text)) > max_hashtags:
        errors.append(f"too many hashtags (> {max_hashtags})")
    if "\n" in text.strip():
        # a single comment should be one block, not a multi-line essay
        if text.count("\n") > 2:
            errors.append("too many line breaks")
    return errors


def generate(llm: LLMClient, post: dict, *, max_chars: int, max_hashtags: int,
             allow_link: bool) -> GenResult:
    """Generate + validate a comment for one post (up to 2 LLM attempts)."""
    prompt = build_prompt(post, max_chars=max_chars, max_hashtags=max_hashtags)
    last_reason = None
    for attempt in range(2):
        try:
            data = llm.analyze_post(prompt, str(Path(post["media_path"]).resolve()))
        except LLMError as exc:
            last_reason = f"llm error: {exc}"
            break
        comment = (data.get("comment") or "").strip()
        metaphor = data.get("metaphor_used")
        errors = validate_comment(
            comment, max_chars=max_chars, max_hashtags=max_hashtags, allow_link=allow_link
        )
        if not errors:
            return GenResult(comment=comment, metaphor=metaphor, status="pending", reason=None)
        last_reason = "; ".join(errors)
        # Tighten the instruction and retry once.
        prompt = (
            prompt
            + f"\n\n# Your previous attempt was rejected: {last_reason}. "
            "Fix it and return ONLY the JSON object again."
        )
    return GenResult(comment=None, metaphor=None, status="skipped", reason=last_reason)
