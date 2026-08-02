"""Production launcher/profile invariants for bots 2 and 3."""

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProdBotMappingTests(unittest.TestCase):
    def test_bot2_launcher_uses_weather_grinder_profile(self):
        launcher = (ROOT / "scripts/run_live_b.sh").read_text(encoding="utf-8")
        self.assertIn("POLYMARKET_PROFILE_LABEL=grinder_b", launcher)
        self.assertIn("auto-loop --live --profile grinder_b --yes", launcher)
        self.assertNotIn("--profile tweet_b", launcher)

    def test_bot2_and_bot3_strategy_profiles_match(self):
        profiles = []
        for name in ("grinder_b.toml", "grinder_c.toml"):
            with (ROOT / "configs/profiles" / name).open("rb") as handle:
                profile = tomllib.load(handle)
            # Account-specific balance metadata is deliberately independent.
            profile["run"].pop("starting_cash")
            profile["sizing"].pop("assumed_live_balance_usd")
            profiles.append(profile)
        self.assertEqual(profiles[0], profiles[1])


if __name__ == "__main__":
    unittest.main()
