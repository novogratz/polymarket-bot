# Claude Development Guide

Read [AGENTS.md](AGENTS.md) before changing this repository. It is the authoritative source for safety rules and current live-strategy behavior.

## Working approach

1. Inspect the selected profile, launcher, settings model, and existing tests.
2. Keep the live scan and selection path deterministic.
3. Add focused tests for every strategy behavior change.
4. Run lint and the complete unit suite.
5. Document customer-visible changes and operational risks.

## Safety

Never display or modify secret values unless the user explicitly requests a local credential operation and the value can remain hidden. Never execute a live order as a test. Do not edit local runtime state while a bot is running.

Useful references:

- [Architecture](docs/ARCHITECTURE.md)
- [Strategy](docs/STRATEGIES.md)
- [Profiles](docs/PROFILES.md)
- [Operations](docs/OPERATIONS.md)
