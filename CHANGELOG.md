# Changelog

All notable changes are documented here. This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic versioning.

## [Unreleased]

### Changed

- Removed the production scanner's 1,500-market client-side ceiling. A zero scan limit now paginates each matching Gamma query until its inventory is exhausted, with stalled-pagination protection.
- Corrected customer and agent documentation to distinguish fresh-entry forecast gates from relaxed held-line redistribution, document the actual FOK price guard, state-file defaults, market-activity gates, and decision-journal semantics.

## [6.0.0] - 2026-08-06

### Added

- Customer-facing architecture, operations, strategy, profile, security, and release documentation.
- Persistent per-scan decision journaling for selected, rejected, and unexecuted weather opportunities.
- Equal-weight full-deployment sizing with portfolio-aware line caps.
- Multi-model Open-Meteo forecast metadata in live decisions and reports.

### Changed

- Standardized all three live profiles on a deterministic, weather-only strategy.
- Require a forecast probability at least two percentage points above the candidate ask for fresh entries.
- Limit entries to asks from 0.90 through 0.97 and markets within six hours of close.
- Hold weather positions to an executable 0.99 bid or settlement; weather stop losses remain disabled.
- Removed spread and local solar-hour gates from weather selection.
- Reworked Telegram reporting to include every open position.
- Replaced fixed stake behavior with portfolio-percentage sizing.

### Removed

- Cryptocurrency markets from live eligibility and live history baselines.
- Random fallback entry and unfiltered live selection.
- Stale experimental documentation and bundled wallet-research reports.

## [5.1.0] - 2026-07-19

### Added

- Full-deployment portfolio sizing and held-line top-ups.

### Changed

- Returned the live weather horizon to 24 hours after evaluation of a longer window.

## [5.0.0] - 2026-07-10

### Added

- Weather as a first-class strategy category.
- Forecast-gated weather profiles and category-aware reporting.

### Changed

- Made weather markets the exclusive live universe.

## [4.0.0] - 2026-07-06

### Changed

- Migrated the three production bots to temperature-market trading.
- Separated offline improvement from deterministic live selection.

## [2.2.0] - 2026-05-08

### Added

- Trade journal, dry-run race tooling, Telegram reports, and strategy profiles.

[Unreleased]: https://github.com/novogratz/polymarket-bot/compare/v6.0.0...HEAD
[6.0.0]: https://github.com/novogratz/polymarket-bot/compare/v5.1.0...v6.0.0
[5.1.0]: https://github.com/novogratz/polymarket-bot/compare/v5.0.0...v5.1.0
[5.0.0]: https://github.com/novogratz/polymarket-bot/compare/v4.0.0...v5.0.0
[4.0.0]: https://github.com/novogratz/polymarket-bot/compare/v2.2.0...v4.0.0
[2.2.0]: https://github.com/novogratz/polymarket-bot/releases/tag/v2.2.0
