# Contributing

Thank you for improving the project. Changes should preserve deterministic live behavior, customer-safe documentation, and an auditable release history.

## Development setup

```bash
git clone git@github.com:novogratz/polymarket-bot.git
cd polymarket-bot
uv sync --extra dev
cp .env.example .env
```

Live credentials are not required for unit tests. Never commit `.env`, runtime data, wallet material, or copied production logs.

## Quality checks

Run before opening a pull request:

```bash
uv run ruff check .
uv run python -B -m unittest discover -s tests
```

For strategy changes, add focused tests that demonstrate the new entry, sizing, or exit rule and its important boundary cases. Prefer standard-library solutions and add environment configuration through the `Settings` dataclass.

## Pull requests

Keep pull requests focused and include:

- The problem and intended behavior.
- Operational or financial risk introduced by the change.
- Tests performed and their results.
- Documentation and changelog updates when behavior changes.
- A rollback approach for production-sensitive changes.

Do not include generated reports containing wallet identifiers or customer data. Do not use real-money execution as a test.

## Commit and release discipline

Use clear, imperative commit subjects. Releases follow semantic versioning and the procedure in [docs/RELEASES.md](docs/RELEASES.md). A release must be built from reviewed `main`, use an annotated tag, and have release notes consistent with the changelog.
