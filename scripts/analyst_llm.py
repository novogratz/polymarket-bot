"""LLM provider helper for analyst sidecars.

The analysts use LLMs only for reports and dry-run TOML proposals. They must
not be part of the market scan or live trade-selection path.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 240


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
    result = subprocess.run(
        ["ollama", "run", model],
        input=prompt,
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
                return _run_codex(prompt, timeout=timeout, cwd=cwd)
            if provider == "ollama":
                return _run_ollama(prompt, timeout=timeout)
            errors.append(f"{provider}: unknown provider")
        except subprocess.TimeoutExpired:
            errors.append(f"{provider}: timeout")
        except Exception as exc:
            errors.append(f"{provider}: {type(exc).__name__}: {exc}")
    detail = "; ".join(errors) if errors else "no providers configured"
    return f"[analyst llm unavailable: {detail}]"
