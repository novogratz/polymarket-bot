"""LLM provider helper for analyst sidecars.

The analysts use LLMs only for reports and dry-run TOML proposals. They must
not be part of the market scan or live trade-selection path.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 240

# Terminal control sequences emitted by `ollama run`'s live renderer.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Reasoning-model chain-of-thought wrapped in <think>...</think>.
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _sanitize_llm_output(text: str) -> str:
    """Strip terminal control codes and reasoning-model 'thinking' so only the
    final answer reaches the report.

    qwen3-style models emit a chain-of-thought — sometimes as <think>...</think>,
    sometimes as a plain 'Thinking...' block — plus ANSI cursor codes. Returning
    empty signals the caller to fall back to its own placeholder.
    """
    if not text:
        return ""
    text = _ANSI_RE.sub("", text).replace("\x1b", "").replace("\r", "")
    text = _THINK_TAG_RE.sub("", text)
    # Keep only what follows the last closing think tag, if any.
    low = text.lower()
    if "</think>" in low:
        text = text[low.rfind("</think>") + len("</think>"):]
    elif "<think>" in low:
        text = text[: low.find("<think>")]
    stripped = text.strip()
    # A plain-text 'Thinking...' preamble with no answer can't be salvaged.
    head = stripped.lower()
    if head.startswith("thinking...") or head.startswith("here's a thinking process"):
        return ""
    return stripped


def _providers() -> list[str]:
    return ["ollama"]


def _run_codex(prompt: str, *, timeout: int, cwd: Path) -> str:
    if shutil.which("codex") is None:
        raise RuntimeError("codex CLI not found")
    model = (os.environ.get("ANALYST_CODEX_MODEL") or "").strip()
    output_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="polymarket-analyst-", suffix=".txt", delete=False) as tmp:
            output_path = tmp.name
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--cd",
            str(cwd),
            "--output-last-message",
            output_path,
        ]
        if model:
            cmd.extend(["--model", model])
        cmd.append("-")
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"codex rc={result.returncode}: {err[:500]}")
        text = Path(output_path).read_text(errors="replace").strip()
        if not text:
            text = (result.stdout or "").strip()
        if not text:
            raise RuntimeError("codex returned empty output")
        return text
    finally:
        if output_path:
            try:
                Path(output_path).unlink()
            except OSError:
                pass


def _run_ollama(prompt: str, *, timeout: int) -> str:
    if shutil.which("ollama") is None:
        raise RuntimeError("ollama CLI not found")
    model = (
        os.environ.get("ANALYST_OLLAMA_MODEL")
        or "fredrezones55/qwen3.6-35b-a3b-uncensored-hauhaucs-aggressive"
    ).strip()
    # `/no_think` disables qwen3's chain-of-thought so we get just the answer.
    result = subprocess.run(
        ["ollama", "run", model],
        input=f"{prompt}\n\n/no_think",
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"ollama rc={result.returncode}: {err[:500]}")
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError("ollama returned empty output")
    return text


def call_analyst_llm(
    prompt: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
) -> str:
    """Return analyst text from Codex first, falling back to Ollama."""
    cwd = cwd or Path.cwd()
    errors: list[str] = []
    for provider in _providers():
        try:
            if provider == "codex":
                return _sanitize_llm_output(_run_codex(prompt, timeout=timeout, cwd=cwd)) or "(verdict unavailable)"
            if provider == "ollama":
                return _sanitize_llm_output(_run_ollama(prompt, timeout=timeout)) or "(verdict unavailable)"
            errors.append(f"{provider}: unknown provider")
        except subprocess.TimeoutExpired:
            errors.append(f"{provider}: timeout")
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {exc}")
    detail = "; ".join(errors) if errors else "no providers configured"
    return f"[analyst llm unavailable: {detail}]"
