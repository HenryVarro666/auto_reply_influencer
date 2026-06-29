"""Multimodal, multi-provider LLM client — Stage 2 brain.

One method, ``analyze_post``, takes a prompt plus a post image and returns the
model's JSON object (the generated comment + self-check fields).

Providers:
  - ``claude-cli`` (DEFAULT): shells out to the ``claude`` command in this
    environment. No API key. The model reads the image with its Read tool, so we
    pass ``--allowedTools Read`` and ``--add-dir <image dir>``.
  - ``gpt``        : OpenAI vision (gpt-4o) — the explicit "second option".
  - ``gemini``     : google-generativeai (gemini-1.5-pro).
  - ``claude-api`` : Anthropic SDK — automatic fallback when the CLI is missing.

Each non-default provider lazy-imports its SDK, so the default path needs no
extra packages installed.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

SUPPORTED = ("claude-cli", "claude-api", "gpt", "gemini")


class LLMError(Exception):
    pass


def _mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}.get(ext, "image/jpeg")


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response (tolerates ``` fences)."""
    if not text:
        raise LLMError("empty model response")
    s = text.strip()
    if s.startswith("```"):
        # strip a leading ```json / ``` fence and trailing ```
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError(f"no JSON object in response: {text[:200]!r}")
    return json.loads(s[start:end + 1])


class LLMClient:
    def __init__(self, provider: str | None = None, model: str | None = None,
                 *, timeout: float = 180.0):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "claude-cli")).lower()
        # claude-cli auto-falls back to claude-api if the binary is absent.
        if self.provider == "claude-cli" and not self._claude_bin():
            self.provider = "claude-api"
        if self.provider not in SUPPORTED:
            raise LLMError(f"unsupported provider {self.provider!r}; choose from {SUPPORTED}")
        self.model = model
        self.timeout = timeout

    # ---- public API --------------------------------------------------------
    def analyze_post(self, prompt: str, image_path: str) -> dict:
        if not Path(image_path).exists():
            raise LLMError(f"image not found: {image_path}")
        if self.provider == "claude-cli":
            return self._claude_cli(prompt, image_path)
        if self.provider == "claude-api":
            return self._claude_api(prompt, image_path)
        if self.provider == "gpt":
            return self._gpt(prompt, image_path)
        if self.provider == "gemini":
            return self._gemini(prompt, image_path)
        raise LLMError(f"unsupported provider {self.provider!r}")

    # ---- claude via terminal CLI (default) ---------------------------------
    @staticmethod
    def _claude_bin() -> str | None:
        return os.getenv("CLAUDE_CLI_BIN") or shutil.which("claude")

    def _claude_cli(self, prompt: str, image_path: str) -> dict:
        bin_ = self._claude_bin()
        if not bin_:
            raise LLMError("claude CLI not found on PATH (set CLAUDE_CLI_BIN or use --provider)")
        img_dir = str(Path(image_path).resolve().parent)
        cmd = [
            bin_, "-p", prompt,
            "--output-format", "json",
            "--allowedTools", "Read",
            "--add-dir", img_dir,
            "--max-turns", "6",
        ]
        if self.model or os.getenv("CLAUDE_CLI_MODEL"):
            cmd += ["--model", self.model or os.environ["CLAUDE_CLI_MODEL"]]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"claude CLI timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise LLMError(f"claude CLI exited {proc.returncode}: {proc.stderr[:300]}")
        # --output-format json wraps the final text in {"result": "..."}.
        try:
            envelope = json.loads(proc.stdout)
            result_text = envelope.get("result", proc.stdout)
        except json.JSONDecodeError:
            result_text = proc.stdout
        return _extract_json(result_text)

    # ---- claude via Anthropic API (fallback) -------------------------------
    def _claude_api(self, prompt: str, image_path: str) -> dict:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise LLMError("provider 'claude-api' needs `pip install anthropic`") from exc
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("claude-api needs ANTHROPIC_API_KEY (or use provider claude-cli)")
        client = anthropic.Anthropic(api_key=key)
        b64 = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
        model = self.model or os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
        msg = client.messages.create(
            model=model,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64",
                                                  "media_type": _mime(image_path), "data": b64}},
                    {"type": "text", "text": prompt},
                ],
            }],
        )
        return _extract_json("".join(b.text for b in msg.content if b.type == "text"))

    # ---- OpenAI GPT (second option) ----------------------------------------
    def _gpt(self, prompt: str, image_path: str) -> dict:
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise LLMError("provider 'gpt' needs `pip install openai`") from exc
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise LLMError("gpt needs OPENAI_API_KEY")
        client = OpenAI(api_key=key)
        b64 = base64.standard_b64encode(Path(image_path).read_bytes()).decode()
        model = self.model or os.getenv("OPENAI_MODEL", "gpt-4o")
        resp = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:{_mime(image_path)};base64,{b64}"}},
                ],
            }],
        )
        return _extract_json(resp.choices[0].message.content)

    # ---- Google Gemini -----------------------------------------------------
    def _gemini(self, prompt: str, image_path: str) -> dict:
        try:
            import google.generativeai as genai  # type: ignore
            from PIL import Image  # type: ignore
        except ImportError as exc:
            raise LLMError("provider 'gemini' needs `pip install google-generativeai Pillow`") from exc
        key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise LLMError("gemini needs GEMINI_API_KEY")
        genai.configure(api_key=key)
        model = genai.GenerativeModel(self.model or os.getenv("GEMINI_MODEL", "gemini-1.5-pro"))
        resp = model.generate_content([prompt, Image.open(image_path)])
        return _extract_json(resp.text)
