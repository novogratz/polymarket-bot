# Profiles

Strategy profiles are TOML files in `configs/profiles/`. They provide reproducible policy defaults while environment variables supply deployment-specific values such as wallet selection, reporting, and runtime cadence.

## Production profiles

| Profile | Typical launcher | Purpose |
| --- | --- | --- |
| `grinder.toml` | `scripts/run_live_70.sh` | Bot 1 weather-only production lane |
| `grinder_b.toml` | `scripts/run_live_b.sh` | Bot 2 weather-only production lane |
| `grinder_c.toml` | `scripts/run_live_c.sh` | Bot 3 weather-only production lane |

All maintained live profiles use the exact core policy in [STRATEGIES.md](STRATEGIES.md): weather-only fresh entries, a 0.02 forecast-edge gate, short-dated selection, percentage sizing, and disabled weather loss exits. A regression test prevents strategy drift. Only the profile-level assumed balance may differ because it is a deployment fallback; maintained launchers override it to 85 unless the environment already supplies a value.

All three production profiles set `race.scan_limit = 0`. In `GammaClient`, zero means paginate the complete matching inventory rather than stop after a fixed number of markets.

## Research profiles

The remaining files are retained for dry-run evaluation and compatibility:

- `baseline.toml` and `baseline_tight.toml` — baseline simulations.
- `copy-wallet.toml` — legacy wallet-following research.
- `live-90.toml` — historical high-probability profile.
- `smart.toml` — legacy smart-money profile.

Do not assume a profile is approved for production because it exists in the repository.

## Configuration precedence

For `pmbot auto-loop`, configuration is resolved in this order:

1. Command-line options select the mode, profile, and dry-run name.
2. Existing non-empty environment variables override matching TOML values.
3. The selected TOML profile fills missing environment values.
4. `Settings` supplies defaults for values absent from both.

For a named dry run, the CLI then injects isolated paths under `data/dry_runs/<run>/` before constructing `Settings`.

Launch scripts set the 85-unit balance fallback, 10-second live interval, disabled daily drawdown gate, 30-minute analyst interval, bot label, alert policy, and profile selection. They do not set isolated live ledgers. Review the selected profile, launcher, existing environment, and generated `data/live_config_snapshot.toml` before deployment.

## Change control

For any profile change that affects entry, sizing, or exit behavior:

1. Update or add focused unit tests.
2. Run the complete test suite.
3. Run the profile in an isolated dry-run ledger.
4. Review decisions, fills, and portfolio caps.
5. Update strategy documentation and the changelog.
6. Promote through a reviewed pull request.

Secrets and wallet credentials never belong in a profile. Store them only in the ignored local `.env` file or an approved secrets manager.
