# Autonomous self-improvement

The bot improves its own **live** trading strategy and opens pull requests for
the changes, driven by the **Claude Code CLI**. It is fenced so it can optimise
exits and sizing but can never disturb the entry selection that drives the win
rate.

## What it does

`scripts/auto_improve.py` (driven by `.github/workflows/auto-improve.yml`):

1. Reads the current EXIT/SIZING parameters from the **live** profile
   `configs/profiles/grinder.toml`.
2. Asks the **Claude Code CLI** (`claude -p`) for a small, bounded improvement
   (falls back to a deterministic hill-climb if the CLI is unavailable).
3. Applies it (section-aware), then runs two audits:
   - `_guard_diff` — abort if any file other than `grinder.toml` changed.
   - `_audit_frozen` — abort if any entry filter or stop-loss key moved.
4. Runs the full unit-test suite. Failure → revert, no PR.
5. Pushes a branch, opens a PR with `gh pr create`, and arms **auto-merge**
   (`gh pr merge --auto --squash`) so it merges once CI is green.

A merged change reaches a live bot the next time that bot restarts and reloads
`grinder.toml`.

## What it can tune vs. what is frozen

**Tunable (exit / sizing only)** — `TUNABLE` in `scripts/auto_improve.py`:

| Key | Range | Meaning |
| --- | --- | --- |
| `race.tp_pct` | 0.05–1.0 | take-profit (1.0 = ride to resolution) |
| `race.stake_pct` | 0.10–0.60 | position size per trade |
| `race.max_orders_per_tick` | 1–5 | concurrency / sizing spread |
| `race.resolved_exit_threshold` | pinned 0.99 | winner exit price (user rule 2026-06-10: winners sell at a real 0.99 bid or settle at 1.00 — never tunable below 0.99) |
| `exits.max_hold_hours` | 1.0–4.5 | max-hold backstop |

**Frozen forever (never tunable):**

- **Entry / bet selection** — price band (`min_price`/`max_price`), `max_spread`,
  `max_hours`, `max_day_change_pct`, `min_outcome_momentum`, liquidity/volume
  floors. This is what produces the win rate, so it is off-limits.
- **Stop-loss** — `sl_pct` / `stop_loss_pct` are never tunable; the agent can
  never introduce a stop-loss (honours "never sell losing positions").
- Every other file: other profiles, `.env`, and all source code.

## Switches

Set as **repo variables** (Settings → Secrets and variables → Actions →
Variables), or env vars locally. Defaults reflect the owner's choice.

| Switch | Default | Effect |
| --- | --- | --- |
| `AUTO_IMPROVE_ENABLED` | `0` | Master gate. Nothing runs unless `1`. |
| `AUTO_IMPROVE_USE_LLM` | `1` | Use the Claude Code CLI to propose. Off → deterministic hill-climb. |
| `AUTO_IMPROVE_AUTOMERGE` | `1` | Auto-merge the PR once CI is green. |

The Claude CLI needs repo secret **`ANTHROPIC_API_KEY`**. For meaningful
auto-merge, add a **branch-protection rule** on `main` requiring the `tests`
check — then a red-CI PR can never merge.

## Safety summary

The win rate is protected because the agent literally cannot edit the entry
filters, and it can never add a stop-loss. The worst it can do is change
take-profit, position size, concurrency, the winner-exit price, or max-hold —
each inside a hard-clamped range, gated by passing tests and green CI. Every
change is a reviewable, revertable commit on `main`.
