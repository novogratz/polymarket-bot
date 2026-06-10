"""Regression tests for issue #30 — the Gamma API silently caps /markets
responses at 100 rows, so get_markets must paginate with offset and treat
the requested limit as a client-side ceiling."""

import unittest
import urllib.parse

from polymarket_bot.gamma import GammaClient


def _page(start: int, count: int):
    return [{"id": str(1000 + i), "question": f"Market {1000 + i}?"} for i in range(start, start + count)]


class _FakeGamma(GammaClient):
    """GammaClient with _get_json replaced by a canned page server that
    mimics the real API's behavior: never more than 100 rows per response."""

    def __init__(self, inventory_size: int, fail_at_offset: int | None = None):
        super().__init__()
        self.inventory_size = inventory_size
        self.fail_at_offset = fail_at_offset
        self.requests: list[dict[str, str]] = []

    def _get_json(self, path: str):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query))
        self.requests.append(query)
        offset = int(query.get("offset", "0"))
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            raise OSError("boom")
        limit = min(int(query["limit"]), 100)  # the silent server cap
        remaining = max(0, self.inventory_size - offset)
        return _page(offset, min(limit, remaining))


class GammaPaginationTests(unittest.TestCase):
    def test_paginates_past_the_100_row_server_cap(self):
        client = _FakeGamma(inventory_size=1900)
        markets = client.get_markets(limit=500)
        self.assertEqual(len(markets), 500)
        self.assertEqual(len({m["id"] for m in markets}), 500)
        offsets = [int(q.get("offset", "0")) for q in client.requests]
        self.assertEqual(offsets, [0, 100, 200, 300, 400])

    def test_short_page_ends_the_inventory(self):
        client = _FakeGamma(inventory_size=140)
        markets = client.get_markets(limit=500)
        self.assertEqual(len(markets), 140)
        self.assertEqual(len(client.requests), 2)

    def test_limit_under_cap_is_a_single_request(self):
        client = _FakeGamma(inventory_size=1900)
        markets = client.get_markets(limit=50)
        self.assertEqual(len(markets), 50)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0]["limit"], "50")

    def test_deduplicates_markets_that_shift_between_pages(self):
        class _Shifting(_FakeGamma):
            def _get_json(self, path):
                payload = super()._get_json(path)
                offset = int(self.requests[-1].get("offset", "0"))
                if offset > 0:
                    # a market slid from the previous page into this one
                    payload[0] = {"id": str(1000 + offset - 1), "question": "dup"}
                return payload

        client = _Shifting(inventory_size=200)
        markets = client.get_markets(limit=200)
        ids = [m["id"] for m in markets]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(markets), 199)  # 200 fetched, 1 duplicate dropped

    def test_later_page_failure_returns_partial_results(self):
        client = _FakeGamma(inventory_size=1900, fail_at_offset=200)
        markets = client.get_markets(limit=500)
        self.assertEqual(len(markets), 200)

    def test_first_page_failure_raises(self):
        client = _FakeGamma(inventory_size=1900, fail_at_offset=0)
        with self.assertRaises(OSError):
            client.get_markets(limit=500)


if __name__ == "__main__":
    unittest.main()
