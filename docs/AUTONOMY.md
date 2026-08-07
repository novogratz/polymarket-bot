# Offline Improvement Process

The self-improvement sidecar analyzes completed trading data outside the live decision path. The maintained launchers enable it by default; set `DAILY_SELF_IMPROVE=0` to disable it.

## Separation from live execution

The live scanner and order engine are deterministic Python. They do not call an LLM. The sidecar polls every 30 minutes and runs once per local calendar day after 23:00 by default. Its tuner uses Claude CLI by default when `AUTO_IMPROVE_USE_LLM=1`.

## Workflow

1. Read journaled decisions, resolved outcomes, and portfolio results.
2. Generate an end-of-day analysis.
3. Propose changes within an explicit file and parameter allowlist.
4. Reject changes outside the permitted scope.
5. Run the complete test suite and validation checks.
6. Revert the proposal if validation fails.
7. Create and push an `auto/tune-grinder-*` branch and open a pull request.
8. By default, arm squash auto-merge; GitHub merges only after required CI succeeds. Set `AUTO_IMPROVE_AUTOMERGE=0` to require manual merge.
9. Restore the checkout to the branch on which the sidecar started. A merged profile change takes effect only after a later bot restart.

## Fences

- Entry-market classification and the no-crypto policy are not autonomously relaxed.
- Weather stop losses cannot be introduced by the sidecar.
- Live credentials and local runtime state are never inputs to generated patches.
- Only `configs/profiles/grinder.toml` can be modified; bots 2 and 3 are not tuned by this process.
- The process may not bypass local tests or required GitHub checks. Auto-merge is enabled by default, so human review is optional unless operators disable it or branch protection requires it.
- A failure in the sidecar must not terminate the live loop.

Treat all generated analysis as a hypothesis. Operators who require human approval must set `AUTO_IMPROVE_AUTOMERGE=0`.
