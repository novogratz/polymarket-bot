"""Tests for polymarket_bot/wallet_resolver.py."""

from __future__ import annotations

import os

os.environ["POLYMARKET_SKIP_DOTENV"] = "1"
for _k in [k for k in os.environ if k.startswith("POLYMARKET_") and k != "POLYMARKET_SKIP_DOTENV"]:
    del os.environ[_k]

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from polymarket_bot import wallet_resolver as wr
from polymarket_bot.smart_money import SmartTrader


class TestIsAddress(unittest.TestCase):
    def test_valid_address(self) -> None:
        self.assertTrue(wr.is_address("0x" + "a" * 40))
        self.assertTrue(wr.is_address("0x" + "F" * 40))

    def test_invalid_address(self) -> None:
        self.assertFalse(wr.is_address("0xABC"))
        self.assertFalse(wr.is_address("not-an-address"))
        self.assertFalse(wr.is_address(""))

    def test_whitespace_trimmed(self) -> None:
        self.assertTrue(wr.is_address("  0x" + "1" * 40 + "  "))


class TestExtractFromUrl(unittest.TestCase):
    def test_extracts_from_polymarket_url(self) -> None:
        url = "https://polymarket.com/profile/0xABCdef0123456789abcdef0123456789ABCdef01"
        addr = wr.extract_address_from_url(url)
        self.assertEqual(addr, "0xabcdef0123456789abcdef0123456789abcdef01")

    def test_returns_none_when_no_address(self) -> None:
        self.assertIsNone(wr.extract_address_from_url("https://polymarket.com/profile/foo"))

    def test_works_with_arbitrary_text(self) -> None:
        # Some embedded address — useful tolerance for copy-pasted URLs.
        addr = wr.extract_address_from_url("see 0x" + "1" * 40 + " for details")
        self.assertEqual(addr, "0x" + "1" * 40)


class TestCacheRoundTrip(unittest.TestCase):
    def test_load_missing_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache = wr.load_cache(Path(td) / "missing.json")
        self.assertEqual(cache, {})

    def test_save_then_load_lowercases_everything(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            wr.save_cache(path, {"BossOskil1": "0xABC", "Swisstony": "0xDEF"})
            loaded = wr.load_cache(path)
        self.assertEqual(loaded, {"bossoskil1": "0xabc", "swisstony": "0xdef"})

    def test_load_ignores_non_dict_payload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "cache.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            loaded = wr.load_cache(path)
        self.assertEqual(loaded, {})


class TestResolveUsername(unittest.TestCase):
    def _trader(self, username: str, wallet: str) -> SmartTrader:
        return SmartTrader(
            wallet=wallet, username=username, pnl=0.0, volume=0.0, category="OVERALL"
        )

    def test_cache_hit_skips_api(self) -> None:
        api = mock.MagicMock()
        cache = {"alice": "0xa1ce"}
        result = wr.resolve_username(
            "Alice", api, cache, sleep_between=0.0, verbose=False
        )
        self.assertEqual(result, "0xa1ce")
        api.leaderboard.assert_not_called()

    def test_resolves_via_leaderboard_and_caches(self) -> None:
        api = mock.MagicMock()
        api.leaderboard.return_value = [
            self._trader("Bob", "0xb0b"),
            self._trader("Alice", "0xa1ce"),
        ]
        cache: dict[str, str] = {}
        result = wr.resolve_username(
            "Alice", api, cache, sleep_between=0.0, verbose=False
        )
        self.assertEqual(result, "0xa1ce")
        self.assertEqual(cache["alice"], "0xa1ce")
        # Side-cache: ``Bob`` also seen, cached for free.
        self.assertEqual(cache["bob"], "0xb0b")

    def test_returns_none_when_not_in_any_leaderboard(self) -> None:
        api = mock.MagicMock()
        api.leaderboard.return_value = [self._trader("Bob", "0xb0b")]
        cache: dict[str, str] = {}
        result = wr.resolve_username(
            "Eve", api, cache, sleep_between=0.0, verbose=False
        )
        self.assertIsNone(result)
        # API was hit for every (category × period) combo before giving up.
        self.assertEqual(
            api.leaderboard.call_count, len(wr._CATEGORIES) * len(wr._PERIODS)
        )

    def test_api_exception_does_not_propagate(self) -> None:
        api = mock.MagicMock()
        api.leaderboard.side_effect = TimeoutError("boom")
        cache: dict[str, str] = {}
        result = wr.resolve_username(
            "Eve", api, cache, sleep_between=0.0, verbose=False
        )
        self.assertIsNone(result)


class TestResolveTarget(unittest.TestCase):
    def test_passes_through_address(self) -> None:
        api = mock.MagicMock()
        addr = "0x" + "9" * 40
        self.assertEqual(
            wr.resolve_target(addr.upper(), api, {}, verbose=False), addr
        )
        api.leaderboard.assert_not_called()

    def test_extracts_from_url(self) -> None:
        api = mock.MagicMock()
        addr = "0x" + "a" * 40
        url = f"https://polymarket.com/profile/{addr.upper()}"
        self.assertEqual(wr.resolve_target(url, api, {}, verbose=False), addr)
        api.leaderboard.assert_not_called()

    def test_falls_back_to_username(self) -> None:
        api = mock.MagicMock()
        cache = {"alice": "0xa1ce"}
        self.assertEqual(
            wr.resolve_target("Alice", api, cache, verbose=False), "0xa1ce"
        )


class TestResolveAll(unittest.TestCase):
    def test_mixes_addresses_urls_and_usernames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            wr.save_cache(cache_path, {"alice": "0xa1ce", "bob": "0xb0b"})
            api = mock.MagicMock()
            raw = (
                f"0x{'1' * 40}, "
                f"https://polymarket.com/profile/0x{'2' * 40}, "
                "Alice, Bob"
            )
            resolved, unresolved = wr.resolve_all(
                raw, cache_path=cache_path, api=api, verbose=False
            )
        self.assertEqual(
            resolved,
            ["0x" + "1" * 40, "0x" + "2" * 40, "0xa1ce", "0xb0b"],
        )
        self.assertEqual(unresolved, [])
        api.leaderboard.assert_not_called()  # all served from cache or parsed inline

    def test_unresolved_returned_separately(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            api = mock.MagicMock()
            api.leaderboard.return_value = []  # never finds anyone
            resolved, unresolved = wr.resolve_all(
                "0x" + "1" * 40 + ", ghost",
                cache_path=cache_path,
                api=api,
                verbose=False,
                sleep_between=0.0,
            )
        self.assertEqual(resolved, ["0x" + "1" * 40])
        self.assertEqual(unresolved, ["ghost"])

    def test_dedupes_resolved_addresses(self) -> None:
        addr = "0x" + "1" * 40
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            wr.save_cache(cache_path, {"alice": addr})
            api = mock.MagicMock()
            raw = f"{addr}, Alice, {addr.upper()}"
            resolved, _ = wr.resolve_all(
                raw, cache_path=cache_path, api=api, verbose=False
            )
        self.assertEqual(resolved, [addr])

    def test_cache_persisted_after_resolution(self) -> None:
        # New username resolved via mocked leaderboard → written to cache.
        with tempfile.TemporaryDirectory() as td:
            cache_path = Path(td) / "cache.json"
            api = mock.MagicMock()
            api.leaderboard.return_value = [
                SmartTrader(
                    wallet="0xc4f3", username="Charlie", pnl=0.0, volume=0.0,
                    category="OVERALL",
                ),
            ]
            wr.resolve_all(
                "Charlie", cache_path=cache_path, api=api, verbose=False,
                sleep_between=0.0,
            )
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["charlie"], "0xc4f3")


if __name__ == "__main__":
    unittest.main()
