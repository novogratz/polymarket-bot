# Autonomous self-improvement

The bot can improve its own trading strategy and open pull requests for the
changes — without breaking the live, money-making bots. This document explains
exactly what is and isn't autonomous, and how to dial the autonomy up.

## What it does

`scripts/auto_improve.py` (driven by `.github/workflows/auto-improve.yml`):

1. Reads the current strategy parameters from the **dry twin** profile
   `configs/profiles/grinder_auto.toml`.
2. Proposes a small, bounded tweak (deterministic hill-climb by default; an
   optional LLM suggestion mode that is still hard-clamped).
3. Applies it, then **fails closed** if anything other than the dry-twin
   profile changed.
4. Runs the full unit-test suite. Test failure → revert, no PR.
5. Pushes a branch and opens a PR with `gh pr create`.
6. Optionally arms GitHub auto-merge (squash) so the PR merges **only when CI
   is green** — off by default.

## What it will never do (hard guarantees)

- **Never touches the live bots.** It edits only `grinder_auto.toml`. Editing
  `grinder.toml`, `grinder_b.toml`, `.env`, or any trade-selection code aborts
  the run (`_guard_diff`).
- **Never spends real money.** `grinder_auto` is a `--dry-run` paper profile.
- **No LLM in the live trade path.** The CLAUDE.md rule stands. The optional
  LLM here runs *offline* and only *suggests* parameter deltas; every value is
  re-clamped to a safe range before use.
- **Respects the 4h-only rule.** `max_hours` is hard-capped at 4.0.
- **No silent live deploy.** Promoting a tuned value from `grinder_auto` to a
  live profile is a manual, human step. The agent proposes; you promote.

Every tunable knob is clamped to `BOUNDS` in `scripts/auto_improve.py`
(price band, spread, hours, day-change, momentum, stake %, orders/tick,
liquidity, volume).

## Switches (off by default)

Set as **repo variables** (Settings → Secrets and variables → Actions →
Variables), or env vars when running locally:

| Switch | Default | Effect |
| --- | --- | --- |
| `AUTO_IMPROVE_ENABLED` | `0` | Master gate. Nothing runs unless `1`. |
| `AUTO_IMPROVE_AUTOMERGE` | `0` | Self-merge the PR once CI is green. Needs a required status check on `main` (branch protection) to be meaningful. |
| `AUTO_IMPROVE_USE_LLM` | `0` | Use the `claude` CLI to propose deltas instead of the deterministic hill-climb. Output is still hard-clamped. |

### Recommended path to real autonomy

1. Run it manually a few times (`AUTO_IMPROVE_ENABLED=1 uv run python
   scripts/auto_improve.py`) and review the PRs.
2. Add a **branch-protection rule** on `main` requiring the `tests` check.
3. Flip `AUTO_IMPROVE_AUTOMERGE=1`. Now good PRs merge themselves; bad ones
   (red CI) never do.
4. Let `grinder_auto` run as a dry twin; when it beats the live config over a
   real sample, copy the winning values into `grinder.toml` yourself.

## Things deliberately left unbuilt

The repo owner asked about two higher-risk capabilities. They are **not**
implemented, by design, because they put real money behind an unsupervised
model:

- **LLM inside the live trade-selection loop** — would add non-determinism,
  latency, and cost to real-money decisions and reverse the core CLAUDE.md
  rule. Strategy ideas from an LLM instead arrive as reviewable PRs.
- **Auto-deploy straight to live** — would let a self-written change reach the
  live bots before any paper-proving. The dry-twin + manual-promotion gate
  exists specifically to prevent this.

If you ever want these, they should be a separate, explicit, well-reviewed
change — not a flag flipped on a good day.
