# Security policy

## Reporting a vulnerability

If you discover a security vulnerability — for example a flaw that could leak credentials, allow unauthorized order placement, or otherwise compromise users running this bot — please do **not** open a public issue.

Instead, contact the maintainer privately by opening a GitHub Security Advisory at <https://github.com/novogratz/polymarket-bot/security/advisories/new>.

When reporting, please include:

- A description of the vulnerability and its potential impact.
- A minimal reproduction (commit hash, environment, command, observed behavior).
- Any suggested mitigation if you have one.

The maintainer will acknowledge the report within a reasonable time and coordinate on a fix and disclosure timeline.

## Scope

This project handles real-money trading credentials. The following are considered in-scope security concerns:

- Code paths that could leak `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`, or any wallet seed.
- Code paths that could place orders without the `--live` flag (or the equivalent `POLYMARKET_ENABLE_LIVE_TRADING=1` env var used internally by scripts).
- Code paths that could place orders without going through the documented consensus / spread / dedupe filters.
- Dependency vulnerabilities that affect the runtime trading path.

## Out of scope

- Strategy underperformance or financial losses from normal market behavior.
- Issues that require a malicious local user with shell access to the host running the bot.
- Issues in third-party services (Polymarket APIs, Coinbase) that this project consumes but does not control.

## Handling secrets

The repository never commits `.env`, private keys, API secrets, or passphrases. Contributors must verify their commits before pushing. If a secret is ever committed by mistake, rotate the credential immediately — `git revert` is not sufficient because the history retains the secret.
