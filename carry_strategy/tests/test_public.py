import unittest
from decimal import Decimal
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from carry_strategy.models import Instrument, Pair
from carry_strategy.public import HOUR, PublicClient, PublicDataError

D = Decimal


def instrument(
    venue="binance", symbol="BTCUSDT", kind="spot", underlying="BTC", size="1"
):
    return Instrument(
        venue,
        symbol,
        kind,
        underlying,
        "USDT",
        D(".1"),
        D(".001"),
        D(".001"),
        D("5"),
        D(size),
    )


def pair(venue="binance"):
    return Pair(
        "pair",
        instrument(venue),
        instrument(venue, kind="linear"),
        D("1"),
        D("1"),
        mapping_verified=True,
    )


class PublicTests(unittest.TestCase):
    def test_okx_contract_book_converts_sizes_to_underlying(self):
        payload = {
            "code": "0",
            "data": [
                {
                    "bids": [["100", "2", "0", "1"]],
                    "asks": [["101", "3", "0", "1"]],
                    "ts": "990",
                }
            ],
        }
        client = PublicClient(transport=lambda url: payload, clock_ms=lambda: 1000)
        contract = instrument("okx", "BTC-USDT-SWAP", "linear", size=".01")
        book = client.book(contract)
        self.assertEqual(book.bids, ((D("100"), D(".02")),))
        self.assertEqual(book.asks[0][1], D(".03"))
        self.assertEqual(book.received_ms, 990)

    def test_auto_discovery_excludes_gold_proxy_and_unapproved_stock(self):
        client = PublicClient()
        instruments = [
            instrument("okx", "XAUT-USDT", underlying="XAUT"),
            instrument("okx", "XAU-USDT-SWAP", "linear", "XAU"),
            instrument("okx", "BTC-USDT"),
            instrument("okx", "BTC-USDT-SWAP", "linear"),
            instrument("okx", "XSNDK-USDT", underlying="XSNDK"),
            instrument("okx", "SNDK-USDT-SWAP", "linear", "SNDK"),
        ]
        client._instruments["okx"] = {(i.kind, i.symbol): i for i in instruments}
        client._inventory_asof["okx"] = client.clock_ms()
        found = client.discover_pairs("OKX", {})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].maker.underlying, "BTC")
        self.assertIsNone(found[0].maker_fee_bps)

    def test_explicit_stock_mapping_retains_readiness_and_internal_route(self):
        client = PublicClient()
        instruments = [
            instrument("bitget", "RSNDKUSDT", underlying="rSNDK"),
            instrument("bitget", "SNDKUSDT", "linear", "SNDK"),
        ]
        client._instruments["bitget"] = {(i.kind, i.symbol): i for i in instruments}
        client._inventory_asof["bitget"] = client.clock_ms()
        found = client.discover_pairs(
            "bitget",
            {"maker_bps": "1", "hedge_bps": "2"},
            {"RSNDKUSDT": {"hedge_symbol": "SNDKUSDT", "underlying": "equity:SNDK"}},
        )
        self.assertEqual(found[0].maker.underlying, found[0].hedge.underlying)
        self.assertFalse(found[0].corporate_actions_ready)
        self.assertEqual(found[0].maker_route, "internal_market")

    def test_unknown_token_share_multiplier_rejected(self):
        client = PublicClient()
        a, b = instrument("okx", "XA-USDT", underlying="XA"), instrument(
            "okx", "A-USDT-SWAP", "linear", "A"
        )
        client._instruments["okx"] = {(i.kind, i.symbol): i for i in (a, b)}
        client._inventory_asof["okx"] = client.clock_ms()
        with self.assertRaisesRegex(ValueError, "share-equivalent"):
            client.discover_pairs(
                "okx",
                {},
                {
                    "XA-USDT": {
                        "hedge_symbol": b.symbol,
                        "underlying": "equity:A",
                        "spot_units_per_share": "1.2",
                    }
                },
            )

    def test_http_ban_is_not_retried(self):
        requests = []

        def failing(url):
            requests.append(url)
            raise HTTPError(url, 418, "blocked", {}, None)

        client = PublicClient(transport=failing)
        with self.assertRaisesRegex(PublicDataError, "HTTP 418"):
            client.book(instrument())
        self.assertEqual(len(requests), 1)

    def test_api_error_cannot_become_empty_book(self):
        client = PublicClient(transport=lambda url: {"code": "429", "msg": "busy"})
        with self.assertRaisesRegex(PublicDataError, "429"):
            client.book(instrument())

    def test_crossed_and_unsorted_books_rejected(self):
        for bids, asks in (
            ([["102", "1"]], [["101", "1"]]),
            ([["99", "1"], ["100", "1"]], [["101", "1"]]),
        ):
            client = PublicClient(transport=lambda url: {"bids": bids, "asks": asks})
            with self.assertRaises(PublicDataError):
                client.book(instrument())

    def test_basis_samples_are_closed_bar_times_with_no_future_bar(self):
        def response(url):
            parsed = urlparse(url)
            px = "101" if parsed.netloc == "fapi.binance.com" else "100"
            return [[t, px, px, px, px] for t in (0, HOUR, 2 * HOUR, 3 * HOUR)]

        client = PublicClient(transport=response)
        result = client.basis_history(pair(), HOUR, 3 * HOUR + 100)
        self.assertEqual([p.timestamp_ms for p in result], [HOUR, 2 * HOUR, 3 * HOUR])
        self.assertEqual([p.basis_bps for p in result], [D("100")] * 3)

    def test_basis_gap_is_not_filled(self):
        client = PublicClient(transport=lambda url: [[0, "100", "100", "100", "100"]])
        with self.assertRaisesRegex(PublicDataError, "missing"):
            client.basis_history(pair(), HOUR, 2 * HOUR)

    def test_binance_candle_history_paginates_forward(self):
        requests = []

        def response(url):
            query = parse_qs(urlparse(url).query)
            first, last = int(query["startTime"][0]), int(query["endTime"][0])
            requests.append((first, last))
            return [
                [t, "100", "100", "100", "100"] for t in range(first, last + 1, HOUR)
            ]

        client = PublicClient(transport=response)
        result = client._hourly_closes(instrument(), 0, 2000 * HOUR)
        self.assertEqual(len(result), 2001)
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[1][0], 1000 * HOUR)

    def test_funding_gap_rejected_even_if_record_count_nonzero(self):
        rows = [
            {"fundingTime": t * HOUR, "fundingRate": ".0001"} for t in (4, 8, 16, 20)
        ]
        client = PublicClient(transport=lambda url: rows)
        client._intervals[("binance", "BTCUSDT")] = 4 * HOUR
        with self.assertRaisesRegex(PublicDataError, "gap"):
            client.funding_history(instrument(kind="linear"), 8 * HOUR, 24 * HOUR)

    def test_regular_funding_and_special_flow_preserved(self):
        rows = [
            {"fundingTime": t * HOUR + 1, "fundingRate": ".0001", "rateType": "Regular"}
            for t in (4, 8, 12, 16, 20)
        ]
        rows += [
            {
                "fundingTime": 12 * HOUR + 2,
                "fundingRate": "-.003",
                "rateType": "Special",
            }
        ]
        client = PublicClient(transport=lambda url: rows)
        client._intervals[("binance", "BTCUSDT")] = 4 * HOUR
        result = client.funding_history(instrument(kind="linear"), 8 * HOUR, 24 * HOUR)
        self.assertEqual(len(result), 5)
        special = [point for point in result if point.kind == "special"]
        self.assertEqual(special[0].rate, D("-.003"))
        self.assertEqual(len([p for p in result if p.kind == "regular"]), 4)

    def test_incomplete_listing_history_rejected(self):
        rows = [{"fundingTime": t * HOUR, "fundingRate": ".0001"} for t in (12, 16, 20)]
        client = PublicClient(transport=lambda url: rows)
        client._intervals[("binance", "BTCUSDT")] = 4 * HOUR
        with self.assertRaisesRegex(PublicDataError, "cover"):
            client.funding_history(instrument(kind="linear"), 8 * HOUR, 24 * HOUR)

    def test_discovery_refresh_observes_delisting(self):
        clock = [1000]
        calls = []

        def response(url):
            calls.append(url)
            query = parse_qs(urlparse(url).query)
            if query["instType"][0] == "SPOT":
                row = dict(
                    instId="BTC-USDT",
                    baseCcy="BTC",
                    quoteCcy="USDT",
                    tickSz=".1",
                    lotSz=".00001",
                    minSz=".00001",
                    state="live",
                )
            else:
                row = dict(
                    instId="BTC-USDT-SWAP",
                    ctType="linear",
                    settleCcy="USDT",
                    uly="BTC-USDT",
                    ctValCcy="BTC",
                    ctVal=".01",
                    ctMult="1",
                    tickSz=".1",
                    lotSz=".01",
                    minSz=".01",
                    state="live" if clock[0] < 2000 else "suspend",
                )
            return {"code": "0", "data": [row]}

        client = PublicClient(
            transport=response, clock_ms=lambda: clock[0], metadata_ttl_ms=1000
        )
        self.assertEqual(len(client.discover_pairs("okx", {})), 1)
        self.assertEqual(len(client.discover_pairs("okx", {})), 1)
        self.assertEqual(len(calls), 2)
        clock[0] = 2000
        self.assertEqual(client.discover_pairs("okx", {}), [])
        self.assertEqual(len(calls), 4)

    def test_bitget_instrument_price_and_quantity_steps(self):
        def response(url):
            if "/spot/" in url:
                return {
                    "code": "00000",
                    "data": [
                        {
                            "symbol": "BTCUSDT",
                            "baseCoin": "BTC",
                            "quoteCoin": "USDT",
                            "pricePrecision": "2",
                            "quantityPrecision": "6",
                            "minTradeAmount": "0",
                            "minTradeUSDT": "5",
                            "status": "online",
                        }
                    ],
                }
            return {
                "code": "00000",
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "symbolType": "perpetual",
                        "supportMarginCoins": ["USDT"],
                        "priceEndStep": "5",
                        "pricePlace": "2",
                        "sizeMultiplier": ".001",
                        "minTradeNum": ".001",
                        "minTradeUSDT": "5",
                        "symbolStatus": "normal",
                        "fundInterval": "8",
                    }
                ],
            }

        client = PublicClient(transport=response)
        found = client.discover_pairs("bitget", {})
        self.assertEqual(found[0].maker.qty_step, D(".000001"))
        self.assertEqual(found[0].hedge.tick_size, D(".05"))
        self.assertEqual(found[0].hedge.base_step, D(".001"))


if __name__ == "__main__":
    unittest.main()
