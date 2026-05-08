.PHONY: help install dev test lint run dashboard journal tune clean

UV ?= uv

help:
	@echo "Available targets:"
	@echo "  install   Sync runtime dependencies into .venv via uv"
	@echo "  dev       Sync runtime + dev dependencies into .venv via uv"
	@echo "  test      Run the unit-test suite"
	@echo "  lint      Run ruff over the codebase"
	@echo "  run       Run the live smart-money loop (foreground)"
	@echo "  dashboard Start the local read-only dashboard"
	@echo "  journal   Print aggregated trade-journal stats"
	@echo "  tune      Run the auto-tuner once and print overrides"
	@echo "  clean     Remove build artefacts and caches"

install:
	$(UV) sync

dev:
	$(UV) sync --extra dev

test:
	$(UV) run python -B -m unittest discover -s tests -v

lint:
	$(UV) run ruff check polymarket_bot tests

run:
	bash scripts/run_live_70.sh

dashboard:
	$(UV) run python -B -m polymarket_bot.main dashboard

journal:
	$(UV) run python -B -m polymarket_bot.main journal-stats

tune:
	$(UV) run python -B -m polymarket_bot.main tune-strategy

clean:
	rm -rf build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
