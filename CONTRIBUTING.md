# Contributing

## Ground rules

- The trading scan path stays deterministic Python over Polymarket APIs. **No LLM call** in scanning or trade-selection code.
- Live trading requires the `--live` flag on `pmbot auto-loop`. The `--yes` flag is for script automation only.
- No random or unfiltered live trade entry. Any new strategy must define explicit entry criteria, spread filters, sizing caps, and duplicate-position guards.
- Every change to strategy behavior must be covered by a unit test.

## Development setup

```bash
pip install -e ".[dev]"   # or: uv sync
```

Run tests:

```bash
uv run python -B -m unittest discover -s tests
```

Run lint:

```bash
ruff check polymarket_bot tests
```

CI runs the same commands on Python 3.11 / 3.12 for every push.

## Pull request checklist

- [ ] Tests added or updated for any strategy change.
- [ ] `uv run python -B -m unittest discover -s tests` passes locally.
- [ ] `ruff check polymarket_bot tests` is clean.
- [ ] User-visible changes have a `CHANGELOG.md` entry.
- [ ] Live config changes update `README.md`, `CLAUDE.md`, `CODEX.md`, and the SKILL files.
- [ ] No secrets, private keys, or `.env` content in commits or commit messages.

## Commit style

- Imperative mood: `Add X`, `Fix Y` — not `Added X`.
- First line ≤ 72 characters.
- Body explains *why*, not just *what*.

## Versioning

Follows [Semantic Versioning](https://semver.org/). User-visible changes require a `CHANGELOG.md` entry. Tag releases on `main`: `git tag -a vX.Y.Z -m "..."`.

## Reporting issues

Open an issue at <https://github.com/novogratz/polymarket-bot/issues> with:

- Expected vs actual behavior.
- CLI command and relevant environment variables (secrets redacted).
- Relevant log output.

For security-sensitive issues, see `SECURITY.md`.
