# Profiles

Strategy profiles are TOML files in `configs/profiles/`. They provide reproducible policy defaults while environment variables supply deployment-specific values such as wallet selection, reporting, and runtime cadence.

## Production profiles

| Profile | Typical launcher | Purpose |
| --- | --- | --- |
| `grinder.toml` | `scripts/run_live_70.sh` | Bot 1 weather-only production lane |
| `grinder_b.toml` | `scripts/run_live_b.sh` | Bot 2 weather-only production lane |
| `grinder_c.toml` | `scripts/run_live_c.sh` | Bot 3 weather-only production lane |

All maintained live profiles use the policy in [STRATEGIES.md](STRATEGIES.md): weather only, forecast gated, short dated, portfolio-percentage sizing, and no weather stop loss.

## Research profiles

The remaining files are retained for dry-run evaluation and compatibility:

- `baseline.toml` and `baseline_tight.toml` — baseline simulations.
- `copy-wallet.toml` — legacy wallet-following research.
- `live-90.toml` — historical high-probability profile.
- `smart.toml` — legacy smart-money profile.

Do not assume a profile is approved for production because it exists in the repository.

## Configuration precedence

At runtime, configuration is resolved in this order:

1. Explicit command-line options.
2. Environment variables.
3. Selected TOML profile.
4. Application defaults.

Launch scripts intentionally set a small number of environment values for isolated ledgers, bot labels, notification channels, and safe automation. Review both the selected profile and launcher before deployment.

## Change control

For any profile change that affects entry, sizing, or exit behavior:

1. Update or add focused unit tests.
2. Run the complete test suite.
3. Run the profile in an isolated dry-run ledger.
4. Review decisions, fills, and portfolio caps.
5. Update strategy documentation and the changelog.
6. Promote through a reviewed pull request.

Secrets and wallet credentials never belong in a profile. Store them only in the ignored local `.env` file or an approved secrets manager.
