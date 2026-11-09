import unittest
from dataclasses import replace
from decimal import Decimal as D

from carry_strategy.models import (
    BasisSample,
    Book,
    FundingPoint,
    Instrument,
    Market,
    Pair,
    SignalConfig,
)
from carry_strategy.signal import (
    DAY_MS,
    HOUR_MS,
    book_vwap,
    evaluate,
    forecast_funding,
    validate_book,
)

NOW = 40 * DAY_MS + 1000


def sample_pair(**changes):
    spot = Instrument(
        "A",
        "GOLDUSDT",
        "spot",
        "GOLD",
        "USDT",
        D(".01"),
        D(".0001"),
        D(".0001"),
        D("5"),
    )
    perp = Instrument(
        "B",
        "GOLDUSDT",
        "linear",
        "GOLD",
        "USDT",
        D(".01"),
        D(".001"),
        D(".001"),
        D("5"),
    )
    pair = Pair("gold", spot, perp, D("1"), D("1"), mapping_verified=True)
    return replace(pair, **changes)


def sample_market(**changes):
    maker = Book(((D("100"), D("100")),), ((D("100.01"), D("100")),), NOW)
    hedge = Book(((D("99.99"), D("100")),), ((D("100"), D("100")),), NOW)
    basis = tuple(
        BasisSample((index + 1) * HOUR_MS, D("-5")) for index in range(39 * 24, 40 * 24)
    )
    funding = tuple(
        FundingPoint(day * DAY_MS, D(".0003"), DAY_MS) for day in range(1, 41)
    )
    market = Market("gold", maker, hedge, basis, funding)
    return replace(market, **changes)


CONFIG = SignalConfig(fair_window=3, deviation_window=5)


class SignalTests(unittest.TestCase):
    def evaluate(self, market=None, pair=None, **kwargs):
        return evaluate(
            pair or sample_pair(),
            market or sample_market(),
            CONFIG,
            NOW,
            D("100000"),
            D("100000"),
            **kwargs
        )

    def test_negative_raw_basis_can_pass_funding_carry(self):
        decision = self.evaluate()
        self.assertTrue(decision.accepted, decision.reason)
        self.assertLess(decision.entry_basis_bps, 0)
        self.assertEqual(decision.maker_price, D("100"))
        self.assertLessEqual(decision.maker_price * decision.maker_qty_base, D("500"))
        self.assertEqual(decision.hedge_qty_base, D("4.999"))
        self.assertEqual(decision.funding_per_day, D(".0003"))
        self.assertGreater(decision.expected_profit, 0)

    def test_depth_vwap_requires_entire_quantity(self):
        book = Book(((D("100"), D("1")), (D("99"), D("4"))), ((D("101"), D("1")),), NOW)
        self.assertEqual(book_vwap(book, "sell", D("5")), D("99.2"))
        self.assertIsNone(book_vwap(book, "SELL", D("5.001")))
        self.assertIsNone(
            book_vwap(replace(book, bids=tuple(reversed(book.bids))), "sell", D("1"))
        )

    def test_expected_profit_matches_full_cycle_cash_flows(self):
        decision = self.evaluate()
        gross_spot = D("5")
        net_spot = gross_spot * D(".9999")
        hedge = D("4.999")
        cash_paid_for_spot = gross_spot * D("100")
        cash_received_on_spot_exit = net_spot * D("100") * D(".9999")
        short_price_profit = hedge * (D("99.99") - D("99.95"))
        short_fees = hedge * (D("99.99") + D("99.95")) * D(".0001")
        funding_received = hedge * D("99.99") * D(".0003") * D("30")
        cost_allowance = D("500") * D(".0004")
        expected = (
            cash_received_on_spot_exit
            - cash_paid_for_spot
            + short_price_profit
            - short_fees
            + funding_received
            - cost_allowance
        )
        self.assertEqual(decision.expected_profit, expected)

    def test_thin_hedge_book_cannot_accept_a_whole_slice(self):
        market = sample_market()
        thin = replace(market.hedge_book, bids=((D("99.99"), D("1")),))
        decision = self.evaluate(replace(market, hedge_book=thin))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "insufficient_hedge_depth")

    def test_fees_and_mapping_are_required(self):
        self.assertEqual(
            self.evaluate(pair=sample_pair(mapping_verified=False)).reason,
            "mapping_unverified",
        )
        self.assertEqual(
            self.evaluate(pair=sample_pair(hedge_fee_bps=None)).reason,
            "account_fees_missing",
        )
        self.assertEqual(
            self.evaluate(pair=sample_pair(corporate_actions_ready=False)).reason,
            "corporate_actions_unresolved",
        )

    def test_stale_future_and_crossed_books_rejected(self):
        market = sample_market()
        self.assertEqual(
            self.evaluate(
                replace(
                    market,
                    maker_book=replace(market.maker_book, received_ms=NOW - 3000),
                )
            ).reason,
            "maker_book_stale",
        )
        self.assertEqual(
            validate_book(replace(market.maker_book, received_ms=NOW + 1), NOW, 2000),
            "book_from_future",
        )
        crossed = replace(market.maker_book, asks=((D("99"), D("10")),))
        self.assertEqual(validate_book(crossed, NOW, 2000), "book_crossed_or_locked")

    def test_missing_funding_interval_cannot_be_treated_as_zero(self):
        market = sample_market()
        points = tuple(p for p in market.funding if p.timestamp_ms != 25 * DAY_MS)
        rate, reason = forecast_funding(points, NOW, DAY_MS)
        self.assertIsNone(rate)
        self.assertEqual(reason, "funding_history_gap_or_overlap")

    def test_daily_forecast_uses_lower_window_and_special_debits(self):
        points = list(sample_market().funding)
        points = [
            replace(p, rate=D(".0006")) if p.timestamp_ms > 33 * DAY_MS else p
            for p in points
        ]
        rate, _ = forecast_funding(points, NOW, DAY_MS)
        self.assertEqual(rate, (D(".0003") * 23 + D(".0006") * 7) / 30)
        points.append(FundingPoint(39 * DAY_MS, D("-.03"), 0, "special"))
        rate, reason = forecast_funding(points, NOW, DAY_MS)
        self.assertEqual(reason, "ok")
        self.assertLess(rate, 0)
        result = self.evaluate(replace(sample_market(), funding=tuple(points)))
        self.assertEqual(result.reason, "nonpositive_carry")
        self.assertLess(result.funding_per_day, 0)

    def test_funding_interval_changes_supported(self):
        points = [
            FundingPoint(day * DAY_MS, D(".0003"), DAY_MS) for day in range(1, 34)
        ]
        points += [
            FundingPoint(33 * DAY_MS + i * 8 * HOUR_MS, D(".0001"), 8 * HOUR_MS)
            for i in range(1, 22)
        ]
        rate, reason = forecast_funding(points, NOW, DAY_MS)
        self.assertEqual(reason, "ok")
        self.assertEqual(rate, D(".0003"))

    def test_duplicate_and_positive_special_funding(self):
        points = list(sample_market().funding)
        rate, reason = forecast_funding(points + [points[-1]], NOW, DAY_MS)
        self.assertIsNone(rate)
        self.assertEqual(reason, "funding_duplicate")
        points.append(FundingPoint(39 * DAY_MS, D(".5"), 0, "special"))
        rate, reason = forecast_funding(points, NOW, DAY_MS)
        self.assertEqual(rate, D(".0003"))

    def test_percentile_bypass_still_checks_economics(self):
        market = sample_market()
        hedge = replace(
            market.hedge_book,
            bids=((D("99.9"), D("100")),),
            asks=((D("99.91"), D("100")),),
        )
        market = replace(market, hedge_book=hedge)
        self.assertEqual(self.evaluate(market).reason, "below_entry_percentile")
        self.assertTrue(self.evaluate(market, require_percentile=False).accepted)
        expensive = sample_pair(maker_fee_bps=D("100"), hedge_fee_bps=D("100"))
        self.assertEqual(
            self.evaluate(market, expensive, require_percentile=False).reason,
            "below_net_edge",
        )

    def test_prior_history_and_hourly_coverage_required(self):
        market = sample_market()
        self.assertEqual(
            self.evaluate(
                replace(market, basis_history=market.basis_history[:3])
            ).reason,
            "basis_history_incomplete",
        )
        points = list(market.basis_history)
        points[-1] = replace(points[-1], timestamp_ms=NOW)
        self.assertEqual(
            self.evaluate(replace(market, basis_history=tuple(points))).reason,
            "basis_history_not_prior",
        )
        points = list(market.basis_history)
        points[-1] = replace(points[-1], timestamp_ms=points[-1].timestamp_ms - HOUR_MS)
        self.assertEqual(
            self.evaluate(replace(market, basis_history=tuple(points))).reason,
            "basis_history_gap_or_order",
        )

    def test_capital_limits_size_and_nondefault_contract_size(self):
        pair = sample_pair(fee_in_base=False)
        decision = evaluate(pair, sample_market(), CONFIG, NOW, D("100000"), D("200"))
        self.assertTrue(decision.accepted)
        self.assertLessEqual(decision.capital_required, D("200"))
        self.assertLess(decision.maker_qty_base, D("5"))
        hedge = replace(
            pair.hedge, qty_step=D("1"), min_qty=D("1"), contract_size=D(".001")
        )
        decision = self.evaluate(pair=replace(pair, hedge=hedge))
        self.assertEqual(decision.hedge_qty_base, D("5"))

    def test_invalid_configuration_is_a_boundary_error(self):
        with self.assertRaises(ValueError):
            evaluate(
                sample_pair(),
                sample_market(),
                replace(CONFIG, horizon_days=D("0")),
                NOW,
                D("500"),
                D("1000"),
            )


if __name__ == "__main__":
    unittest.main()
