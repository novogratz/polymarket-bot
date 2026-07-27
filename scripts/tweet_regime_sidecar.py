#!/usr/bin/env python3
"""Tweet-regime sidecar — OFFLINE, LOCAL OLLAMA ONLY (user 2026-07-27:
"for the llm in the loop i only want to use ollama as local model").

Every TWEET_REGIME_INTERVAL_SECONDS (default 15 min) this sidecar:
  1. pulls each tracked handle's recent posts from xtracker (public feed);
  2. asks a LOCAL Ollama model to classify the current posting regime
     (surge / normal / quiet — e.g. a launch day, a political fight, or a
     travel-quiet spell) and propose an intensity multiplier;
  3. atomically writes data/tweet_regime.json.

The LIVE trade loop NEVER calls an LLM: polymarket_bot/tweet_model.py only
READS the file, ignores it when stale (>2h) and clamps the multiplier to
[0.5, 2.0], so the worst a bad LLM answer can do is scale the intensity
prior by 2x — the deterministic edge gate still decides every trade.

Fail-safe by construction: every cycle is fully wrapped; Ollama down, bad
JSON, xtracker down — nothing is written and the model falls back to 1.0.
This process can NEVER crash or stall the live loop (separate process,
separate data path). Toggle with TWEET_REGIME_SIDECAR=0 in the launcher.

Env:
  OLLAMA_URL                     default http://127.0.0.1:11434
  OLLAMA_MODEL                   default qwen2.5:7b
  TWEET_REGIME_INTERVAL_SECONDS  default 900
  POLYMARKET_TWEET_REGIME_FILE   default data/tweet_regime.json
  TWEET_REGIME_HANDLES           default elonmusk (comma-separated)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

XTRACKER_BASE = "https://xtracker.polymarket.com/api"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
INTERVAL_S = int(os.environ.get("TWEET_REGIME_INTERVAL_SECONDS", "900"))
REGIME_FILE = os.environ.get("POLYMARKET_TWEET_REGIME_FILE", "data/tweet_regime.json")
# NOTE: xtracker handles are CASE-SENSITIVE (/users/ZelenskyyUa works,
# /users/zelenskyyua 404s) — keep them exactly as typed / as the API returns.
HANDLES = [h.strip() for h in os.environ.get("TWEET_REGIME_HANDLES", "auto").split(",") if h.strip()]


def _resolve_handles() -> list[str]:
    """'auto' → every xtracker account with at least one ACTIVE counting
    window (the count model prices all of them, so the regime layer should
    watch all of them too). Fail-safe: elonmusk alone."""
    if HANDLES != ["auto"]:
        return HANDLES
    try:
        users = _get_json(f"{XTRACKER_BASE}/users").get("data") or []
        auto = [
            str(u.get("handle") or "")
            for u in users
            if any(t.get("isActive") for t in (u.get("trackings") or ()))
        ]
        return [h for h in auto if h] or ["elonmusk"]
    except Exception:
        return ["elonmusk"]

_PROMPT = """You are monitoring the posting activity of the X account @{handle}.
Here are their posts from the last 48 hours (newest first), one per line:

{posts}

Recent baseline: this account averaged {baseline:.0f} posts/day over the last 30 days;
the last 48h had {recent} posts.

Classify the CURRENT posting regime and predict the intensity over the NEXT 2-3 days.
Consider signals in the content: product launches, political fights, breaking news
engagement, announced travel/absence, spree patterns.

Answer with ONLY a JSON object, no other text:
{{"regime": "surge" | "normal" | "quiet", "multiplier": <float 0.5-2.0, expected
posting rate over the next 2-3 days relative to the seasonal baseline>,
"reason": "<one short sentence>"}}"""


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-bot/regime-sidecar"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _ollama_chat(prompt: str) -> dict:
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return json.loads(payload["message"]["content"])


def _analyze(handle: str) -> dict | None:
    posts = _get_json(f"{XTRACKER_BASE}/users/{handle}/posts").get("data") or []
    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)
    cutoff_30d = now - timedelta(days=30)
    recent, n_30d = [], 0
    for p in posts:
        try:
            created = datetime.fromisoformat(str(p["createdAt"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if created >= cutoff_30d:
            n_30d += 1
        if created >= cutoff_48h:
            recent.append(str(p.get("content") or "").replace("\n", " ")[:200])
    if n_30d == 0:
        return None
    prompt = _PROMPT.format(
        handle=handle,
        posts="\n".join(recent[:150]) or "(no posts in the last 48h)",
        baseline=n_30d / 30.0,
        recent=len(recent),
    )
    verdict = _ollama_chat(prompt)
    mult = max(0.5, min(2.0, float(verdict.get("multiplier", 1.0))))
    return {
        "regime": str(verdict.get("regime", "normal"))[:20],
        "multiplier": mult,
        "reason": str(verdict.get("reason", ""))[:300],
        "model": OLLAMA_MODEL,
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _write_atomic(data: dict) -> None:
    os.makedirs(os.path.dirname(REGIME_FILE) or ".", exist_ok=True)
    tmp = REGIME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, REGIME_FILE)


def main() -> None:
    print(f"[tweet-regime] sidecar up: model={OLLAMA_MODEL} url={OLLAMA_URL} "
          f"handles={HANDLES} every {INTERVAL_S}s -> {REGIME_FILE}", flush=True)
    while True:
        try:
            handles = _resolve_handles()
            current: dict = {}
            try:
                with open(REGIME_FILE, encoding="utf-8") as fh:
                    current = json.load(fh)
            except Exception:
                current = {}
            wrote = False
            for handle in handles:
                try:
                    entry = _analyze(handle)
                    if entry is not None:
                        # File keys are lowercase — tweet_model lowercases at
                        # lookup; the fetch above keeps the exact-case handle.
                        current[handle.lower()] = entry
                        wrote = True
                        print(f"[tweet-regime] {handle}: {entry['regime']} x{entry['multiplier']:.2f} "
                              f"— {entry['reason']}", flush=True)
                except Exception as exc:
                    print(f"[tweet-regime] {handle}: cycle failed (fail-safe, no write): {exc}", flush=True)
            if wrote:
                _write_atomic(current)
        except Exception as exc:  # belt and suspenders — the loop never dies
            print(f"[tweet-regime] cycle error (ignored): {exc}", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
