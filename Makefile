.PHONY: help install dev test lint run dashboard journal tune clean

PYTHON ?= python3

help:
	@echo "Available targets:"
	@echo "  install   Install runtime dependencies"
	@echo "  dev       Install in editable mode with dev extras"
	@echo "  test      Run the unit-test suite"
	@echo "  lint      Run ruff over the codebase"
	@echo "  run       Run the live smart-money loop (foreground)"
	@echo "  dashboard Start the local read-only dashboard"
	@echo "  journal   Print aggregated trade-journal stats"
	@echo "  tune      Run the auto-tuner once and print overrides"
	@echo "  clean     Remove build artefacts and caches"

install:
	$(PYTHON) -m pip install -r requirements.txt

dev:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -B -m unittest discover -s tests -v

lint:
	$(PYTHON) -m ruff check polymarket_bot tests

run:
	bash scripts/run_live_70.sh

dashboard:
	$(PYTHON) -B -m polymarket_bot.main dashboard

journal:
	$(PYTHON) -B -m polymarket_bot.main journal-stats

tune:
	$(PYTHON) -B -m polymarket_bot.main tune-strategy

clean:
	rm -rf build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
