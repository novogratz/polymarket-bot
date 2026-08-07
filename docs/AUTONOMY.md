# Offline Improvement Process

The optional self-improvement sidecar analyzes completed trading data outside the live decision path. It is intended to produce reviewable proposals, not to grant an AI unrestricted control over live trading.

## Separation from live execution

The live scanner and order engine are deterministic Python. They do not call an LLM. The offline process runs on a schedule after trading data has been persisted and can be disabled with `DAILY_SELF_IMPROVE=0`.

## Workflow

1. Read journaled decisions, resolved outcomes, and portfolio results.
2. Generate an end-of-day analysis.
3. Propose changes within an explicit file and parameter allowlist.
4. Reject changes outside the permitted scope.
5. Run the complete test suite and validation checks.
6. Preserve the current branch and working tree if validation fails.
7. Leave a reviewable change; do not silently alter the running process.

## Fences

- Entry-market classification and the no-crypto policy are not autonomously relaxed.
- Weather stop losses cannot be introduced by the sidecar.
- Live credentials and local runtime state are never inputs to generated patches.
- The process may not bypass tests, live confirmation, or Git review.
- A failure in the sidecar must not terminate the live loop.

Treat all generated analysis as a hypothesis. Promote a change only after sufficient out-of-sample evidence and human review.
