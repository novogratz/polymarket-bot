# Contributing

Thanks for your interest in contributing.

## Ground rules

- The trading scan path stays deterministic Python over Polymarket APIs. **No LLM call** is permitted in the scanning or trade-selection code.
- Live trading must remain gated by `POLYMARKET_ENABLE_LIVE_TRADING=1`.
- Random or unfiltered live trade entry is not accepted. Any new live strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position checks.
- Strategy adjustments at runtime are data files (`data/strategy_overrides.json`), never code rewrites. The bot must not gain the capability to commit or push source code.
- Every change to strategy behavior must be covered by a unit test in `tests/test_strategy.py`.

## Development setup

```bash
python3 -m pip install -e ".[dev]"
```

Run tests:

```bash
python3 -B -m unittest discover -s tests
```

Run lint:

```bash
ruff check polymarket_bot tests
```

CI runs the same commands on Python 3.11 / 3.12 against every push.

## Pull request checklist

- [ ] Tests added or updated for any strategy change.
- [ ] `python3 -B -m unittest discover -s tests` passes locally.
- [ ] `ruff check polymarket_bot tests` is clean.
- [ ] If the change is user-visible (CLI, env var, behavior), `CHANGELOG.md` is updated.
- [ ] If the change touches the live config, `scripts/run_live_70.sh` and the docs (`README.md`, `CLAUDE.md`, `CODEX.md`, the SKILL files) are updated to match.
- [ ] No secrets, private keys, or `.env` content in commits, code, or commit messages.

## Commit style

- Imperative mood (`Add X`, `Fix Y`, not `Added X`).
- First line ≤ 72 characters.
- Body explains *why* the change is needed, not just *what* changed.
- Reference the related issue or commit when relevant.

## Versioning

The project follows [Semantic Versioning](https://semver.org/). User-visible behavior changes require a CHANGELOG entry. Tag a release on `main` once a version is ready: `git tag -a vX.Y.Z -m "..."`.

## Reporting issues

Open an issue at <https://github.com/novogratz/polymarket-bot/issues> with:

- A description of the expected vs actual behavior.
- The environment variables and CLI command that reproduce the issue (with secrets redacted).
- Relevant log output.

For security-sensitive issues, see `SECURITY.md` instead.
