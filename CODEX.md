# Codex Development Guide

Use the repository instructions in [AGENTS.md](AGENTS.md) and the structured skill at `.codex/skills/polymarket-bot/SKILL.md` for Polymarket strategy, filter, sizing, exit, journal, or auto-tuner work.

## Required checks

```bash
uv run ruff check .
uv run python -B -m unittest discover -s tests
```

Inspect `git status` before editing and preserve unrelated work. Use focused patches, keep secrets out of output, and do not use live execution for verification.

Customer and operator references:

- [Architecture](docs/ARCHITECTURE.md)
- [Strategy](docs/STRATEGIES.md)
- [Operations](docs/OPERATIONS.md)
- [Security](SECURITY.md)
