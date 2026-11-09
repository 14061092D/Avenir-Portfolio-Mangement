import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from carry_strategy.engine import Engine
from carry_strategy.models import (
    BasisSample,
    Book,
    EngineConfig,
    FundingPoint,
    Instrument,
    Market,
    Pair,
    SignalConfig,
)

DAY = 86400000
HOUR = 3600000
NOW = 40 * DAY + 1000


def pair(name="gold", maker_venue="A", hedge_venue="B"):
    asset = name.upper()
    maker = Instrument(
        maker_venue,
        asset + "USDT",
        "spot",
        asset,
        "USDT",
        D(".01"),
        D(".0001"),
        D(".0001"),
        D("5"),
    )
    hedge = Instrument(
        hedge_venue,
        asset + "USDT",
        "linear",
        asset,
        "USDT",
        D(".01"),
        D(".001"),
        D(".001"),
        D("5"),
    )
    return Pair(name, maker, hedge, D("1"), D("1"), mapping_verified=True)


def market(name="gold", now=NOW):
    basis = tuple(
        BasisSample((index + 1) * HOUR, D("-5")) for index in range(39 * 24, 40 * 24)
    )
    funding = tuple(FundingPoint(day * DAY, D(".0003"), DAY) for day in range(1, 41))
    return Market(
        name,
        Book(((D("100"), D("100")),), ((D("100.01"), D("100")),), now),
        Book(((D("99.99"), D("100")),), ((D("100"), D("100")),), now),
        basis,
        funding,
    )


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite"
        self.pairs = [pair()]
        self.config = EngineConfig(
            signal=SignalConfig(fair_window=3, deviation_window=5)
        )
        self.engine = Engine(self.path, self.pairs, self.config)

    def tearDown(self):
        self.engine.close()
        self.directory.cleanup()

    def reconcile(self, now=NOW, positions=None, orders=None):
        positions = positions or {
            p.pair_id: dict(spot="0", short="0") for p in self.pairs
        }
        return self.engine.reconcile(
            positions, orders or {}, {"A": "100000", "B": "100000"}, now
        )

    def places(self, side=None, symbol=None):
        rows = [c for c in self.engine.pending_commands() if c["kind"] == "place"]
        if side is not None:
            rows = [c for c in rows if c["side"] == side]
        if symbol is not None:
            rows = [c for c in rows if c["symbol"] == symbol]
        return rows

    def start_entry(self, now=NOW):
        self.assertTrue(self.reconcile(now))
        self.engine.on_market(market(now=now), now)
        maker = self.places("BUY")[0]
        self.assertEqual(maker["time_in_force"], "PostOnly")
        self.assertTrue(self.engine.mark_dispatched(maker["command_id"], now))
        self.engine.on_order_update(maker["client_order_id"], "open", "0", now)
        return maker

    def fill_maker(self, maker, quantity="2", fee=".0002", event="spot-1", now=NOW + 1):
        return self.engine.on_fill(
            "A", event, maker["client_order_id"], quantity, "100", fee, "base", now
        )

    def test_startup_requires_coherent_reconciliation(self):
        self.engine.on_market(market(), NOW)
        self.assertFalse(self.places())
        self.assertEqual(self.engine.pending_commands()[0]["kind"], "reconcile")
        self.assertFalse(self.reconcile(positions={"gold": dict(spot="1", short="0")}))
        self.engine.tick(NOW)
        self.assertFalse(self.places())
        self.assertTrue(self.reconcile())
        self.engine.tick(NOW)
        self.assertEqual(len(self.places()), 1)

    def test_maker_fill_net_of_base_fee_is_hedged_and_deduplicated(self):
        maker = self.start_entry()
        self.assertTrue(self.fill_maker(maker))
        short = self.places("SELL")[0]
        self.assertEqual(short["qty_base"], "1.999")
        self.assertEqual(short["time_in_force"], "IOC")
        self.assertFalse(short["reduce_only"])
        position = self.engine.view()["pairs"]["gold"]
        self.assertEqual(D(position["spot"]), D("1.9998"))
        self.assertEqual(D(position["short"]), 0)
        count = len(self.engine.pending_commands())
        self.assertFalse(self.fill_maker(maker))
        self.assertEqual(len(self.engine.pending_commands()), count)
        self.assertEqual(self.engine.view()["pairs"]["gold"], position)
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            self.fill_maker(maker, quantity="1", event="spot-1")

    def test_hedge_ack_never_updates_inventory_or_duplicates_order(self):
        maker = self.start_entry()
        self.fill_maker(maker)
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_order_update(hedge["client_order_id"], "open", "0", NOW + 2)
        self.engine.tick(NOW + 3)
        self.assertEqual(D(self.engine.view()["pairs"]["gold"]["short"]), 0)
        self.assertFalse(self.places("SELL"))

    def test_terminal_ioc_waits_for_reported_execution_then_retries_only_remainder(
        self,
    ):
        maker = self.start_entry()
        self.fill_maker(maker)
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_order_update(hedge["client_order_id"], "canceled", "1", NOW + 2)
        self.engine.tick(NOW + 3)
        self.assertFalse(self.places("SELL"))
        self.engine.on_fill(
            "B",
            "hedge-partial",
            hedge["client_order_id"],
            "1",
            "99.99",
            ".009999",
            "quote",
            NOW + 4,
        )
        self.engine.tick(NOW + 1 + self.config.hedge_retry_ms)
        retries = self.places("SELL")
        self.assertEqual(len(retries), 1)
        self.assertEqual(D(retries[0]["qty_base"]), D(".999"))
        self.assertEqual(D(self.engine.view()["pairs"]["gold"]["short"]), D("1"))

    def test_late_fill_after_maker_cancel_still_generates_hedge(self):
        maker = self.start_entry()
        self.engine.on_order_update(maker["client_order_id"], "canceled", "0", NOW + 1)
        self.fill_maker(maker, now=NOW + 2)
        self.assertEqual(D(self.engine.view()["pairs"]["gold"]["spot"]), D("1.9998"))
        self.assertEqual(len(self.places("SELL")), 1)
        self.engine.tick(NOW + 3)
        self.assertFalse(self.places("BUY"))

    def test_stale_market_cancels_resting_maker(self):
        maker = self.start_entry()
        self.engine.tick(NOW + 3000)
        cancel = [c for c in self.engine.pending_commands() if c["kind"] == "cancel"]
        self.assertEqual(len(cancel), 1)
        self.assertEqual(cancel[0]["client_order_id"], maker["client_order_id"])
        self.engine.tick(NOW + 4000)
        self.assertEqual(
            len([c for c in self.engine.pending_commands() if c["kind"] == "cancel"]), 1
        )

    def test_unknown_submitted_order_enters_reconciliation_without_new_place(self):
        maker = self.start_entry()
        self.engine.on_order_update(maker["client_order_id"], "unknown", "0", NOW + 1)
        self.engine.on_market(market(now=NOW + 2), NOW + 2)
        self.assertFalse(self.engine.view()["pairs"]["gold"]["ready"])
        self.assertFalse(self.places())
        self.assertTrue(
            any(c["kind"] == "reconcile" for c in self.engine.pending_commands())
        )

    def test_unwind_reduces_short_after_spot_exit_fill(self):
        maker = self.start_entry()
        self.fill_maker(maker, quantity="5", fee=".0005")
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_fill(
            "B",
            "hedge-full",
            hedge["client_order_id"],
            "4.999",
            "99.99",
            ".049985001",
            "quote",
            NOW + 2,
        )
        self.engine.request_exit("gold", NOW + 3)
        exits = [c for c in self.places("SELL") if c["venue"] == "A"]
        self.assertEqual(len(exits), 1)
        exit_order = exits[0]
        self.assertEqual(exit_order["time_in_force"], "PostOnly")
        self.engine.mark_dispatched(exit_order["command_id"], NOW + 3)
        self.engine.on_fill(
            "A",
            "spot-exit",
            exit_order["client_order_id"],
            exit_order["qty_base"],
            exit_order["price"],
            ".04999",
            "quote",
            NOW + 4,
        )
        self.engine.tick(NOW + 1 + self.config.hedge_retry_ms)
        buys = [c for c in self.places("BUY") if c["venue"] == "B"]
        self.assertEqual(len(buys), 1)
        self.assertTrue(buys[0]["reduce_only"])
        self.assertLessEqual(D(buys[0]["qty_base"]), D("4.999"))

    def test_shared_capital_is_not_overbooked_by_two_initial_makers(self):
        self.engine.close()
        self.pairs = [pair(), pair("silver")]
        self.config = replace(self.config, capital_limit=D("1000"))
        self.engine = Engine(self.path, self.pairs, self.config)
        self.assertTrue(self.reconcile())
        self.engine.on_market(market("gold"), NOW)
        self.engine.on_market(market("silver"), NOW)
        promised = sum(
            D(order["capital_reserve"])
            for order in self.engine.view()["orders"].values()
        )
        self.assertLessEqual(promised, D("1000"))

    def test_restart_cannot_dispatch_old_unsubmitted_maker_before_reconciliation(self):
        self.assertTrue(self.reconcile())
        self.engine.on_market(market(), NOW)
        old = self.places("BUY")[0]
        self.engine.close()
        self.engine = Engine(self.path, self.pairs, self.config)
        self.assertFalse(self.engine.mark_dispatched(old["command_id"], NOW + 1))

    def test_known_position_mismatch_cannot_create_a_new_hedge(self):
        maker = self.start_entry()
        self.fill_maker(maker)
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_order_update(hedge["client_order_id"], "rejected", "0", NOW + 2)
        positions = {"gold": dict(spot="1.9998", short="1.999")}
        self.assertFalse(
            self.engine.reconcile(
                positions, {}, {"A": "100000", "B": "100000"}, NOW + 3
            )
        )
        later = NOW + 1 + self.config.hedge_retry_ms
        self.engine.on_market(market(now=later), later)
        self.assertFalse(self.places("SELL"))

    def test_pending_hedge_margin_remains_reserved_for_other_pairs(self):
        self.engine.close()
        self.pairs = [pair(), pair("silver")]
        self.config = replace(self.config, capital_limit=D("1000"))
        self.engine = Engine(self.path, self.pairs, self.config)
        maker = self.start_entry()
        self.fill_maker(maker, quantity="5", fee=".0005")
        self.engine.on_market(market("silver", NOW + 2), NOW + 2)
        state = self.engine.view()
        committed = sum(
            D(p["spot_cost"]) + D(p["margin"]) for p in state["pairs"].values()
        )
        for order in state["orders"].values():
            if order["status"] in {"filled", "canceled", "rejected"}:
                continue
            unfilled = D(order["qty"]) - D(order["filled"])
            if order["leg"] == "maker" and order["side"] == "BUY":
                committed += D(order["capital_reserve"]) * unfilled / D(order["qty"])
            if order["leg"] == "hedge" and order["side"] == "SELL":
                committed += unfilled * D(order["price"]) * D(".5")
        self.assertLessEqual(committed, D("1000"))

    def test_depth_shrink_after_maker_fill_still_allows_bounded_risk_reduction(self):
        maker = self.start_entry()
        thin = market(now=NOW + 1)
        thin = replace(
            thin, hedge_book=replace(thin.hedge_book, bids=((D("99.99"), D("1")),))
        )
        self.engine.on_market(thin, NOW + 1)
        self.fill_maker(maker, quantity="5", fee=".0005", now=NOW + 2)
        hedges = self.places("SELL")
        self.assertEqual(len(hedges), 1)
        self.assertGreater(D(hedges[0]["qty_base"]), 0)
        self.assertLessEqual(D(hedges[0]["qty_base"]), D("4.999"))
        self.assertGreaterEqual(D(hedges[0]["price"]), D("99.89"))
        self.assertFalse(self.places("BUY"))

    def test_entry_depth_check_includes_existing_rounding_dust(self):
        maker = self.start_entry()
        self.fill_maker(maker, quantity="5", fee=".0005")
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_fill(
            "B",
            "hedge-full",
            hedge["client_order_id"],
            "4.999",
            "99.99",
            ".049985001",
            "quote",
            NOW + 2,
        )
        thin = market(now=NOW + 3)
        thin = replace(
            thin, hedge_book=replace(thin.hedge_book, bids=((D("99.99"), D("4.999")),))
        )
        self.engine.on_market(thin, NOW + 3)
        for candidate in self.places("BUY"):
            current = self.engine.view()["pairs"]["gold"]
            projected_delta = (
                D(current["spot"])
                - D(current["short"])
                + D(candidate["qty_base"]) * D(".9999")
            )
            hedge_units = (projected_delta / D(".001")).to_integral_value(
                rounding="ROUND_DOWN"
            ) * D(".001")
            self.assertLessEqual(hedge_units, D("4.999"))

    def test_filled_status_requires_full_cumulative_quantity(self):
        maker = self.start_entry()
        with self.assertRaises(ValueError):
            self.engine.on_order_update(
                maker["client_order_id"], "filled", "0", NOW + 1
            )
        self.assertEqual(
            self.engine.view()["orders"][maker["client_order_id"]]["status"], "open"
        )

    def test_untradeable_dust_does_not_consume_active_pair_slot(self):
        self.engine.close()
        self.pairs = [pair(), pair("silver")]
        self.config = replace(self.config, max_active_pairs=1)
        self.engine = Engine(self.path, self.pairs, self.config)
        maker = self.start_entry()
        self.fill_maker(maker, quantity="1.2345", fee=".00012345")
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_fill(
            "B",
            "hedge-open",
            hedge["client_order_id"],
            "1.234",
            "99.99",
            ".012338766",
            "quote",
            NOW + 2,
        )
        self.engine.on_order_update(
            maker["client_order_id"], "canceled", "1.2345", NOW + 3
        )
        self.engine.request_exit("gold", NOW + 4)
        sell = [c for c in self.places("SELL") if c["venue"] == "A"][0]
        self.engine.mark_dispatched(sell["command_id"], NOW + 4)
        self.engine.on_fill(
            "A",
            "spot-close",
            sell["client_order_id"],
            sell["qty_base"],
            sell["price"],
            ".0123442343",
            "quote",
            NOW + 5,
        )
        later = NOW + 1 + self.config.hedge_retry_ms
        self.engine.on_market(market(now=later), later)
        cover = [c for c in self.places("BUY") if c["venue"] == "B"][0]
        self.engine.mark_dispatched(cover["command_id"], later)
        self.engine.on_fill(
            "B",
            "hedge-close",
            cover["client_order_id"],
            cover["qty_base"],
            "100",
            ".01234",
            "quote",
            later + 1,
        )
        gold = self.engine.view()["pairs"]["gold"]
        self.assertGreater(D(gold["spot"]), 0)
        self.assertFalse(gold["campaign"])
        self.engine.on_market(market("silver", later + 2), later + 2)
        self.assertTrue(self.places("BUY", "SILVERUSDT"))

    def test_stale_terminal_update_cannot_erase_expected_hedge_fills(self):
        maker = self.start_entry()
        self.fill_maker(maker)
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_order_update(
            hedge["client_order_id"], "filled", hedge["qty_base"], NOW + 2
        )
        self.engine.on_order_update(hedge["client_order_id"], "canceled", "0", NOW + 3)
        self.engine.on_market(market(now=NOW + 1002), NOW + 1002)
        orders = [
            o for o in self.engine.view()["orders"].values() if o["leg"] == "hedge"
        ]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["reported_filled"], hedge["qty_base"])

    def test_reconciliation_requeues_uncertain_cancel_of_open_order(self):
        maker = self.start_entry()
        self.engine.tick(NOW + 3000)
        cancel = next(
            c for c in self.engine.pending_commands() if c["kind"] == "cancel"
        )
        self.engine.mark_dispatched(cancel["command_id"], NOW + 3001)
        self.assertFalse(
            any(c["kind"] == "cancel" for c in self.engine.pending_commands())
        )
        self.assertTrue(
            self.reconcile(
                NOW + 3002,
                orders={
                    maker["client_order_id"]: dict(
                        status="open", cumulative_qty_base="0"
                    )
                },
            )
        )
        retried = [c for c in self.engine.pending_commands() if c["kind"] == "cancel"]
        self.assertEqual([c["command_id"] for c in retried], [cancel["command_id"]])

    def test_exit_closes_short_against_bounded_subminimum_spot_dust(self):
        maker = self.start_entry()
        self.fill_maker(maker, quantity="5", fee=".0005")
        hedge = self.places("SELL")[0]
        self.engine.mark_dispatched(hedge["command_id"], NOW + 1)
        self.engine.on_fill(
            "B",
            "hedge-open",
            hedge["client_order_id"],
            "4.999",
            "99.99",
            ".05",
            "quote",
            NOW + 2,
        )
        self.engine.request_exit("gold", NOW + 3)
        sell = next(c for c in self.places("SELL") if c["venue"] == "A")
        self.engine.mark_dispatched(sell["command_id"], NOW + 3)
        self.engine.on_fill(
            "A",
            "exit-part",
            sell["client_order_id"],
            "4.9595",
            "100.01",
            ".05",
            "quote",
            NOW + 4,
        )
        self.engine.on_order_update(
            sell["client_order_id"], "canceled", "4.9595", NOW + 5
        )
        self.engine.on_market(market(now=NOW + 1002), NOW + 1002)
        buy = next(c for c in self.places("BUY") if c["venue"] == "B")
        self.assertEqual(D(buy["qty_base"]), D("4.999"))
        self.engine.mark_dispatched(buy["command_id"], NOW + 1002)
        self.engine.on_fill(
            "B",
            "hedge-close",
            buy["client_order_id"],
            buy["qty_base"],
            "100",
            ".05",
            "quote",
            NOW + 1003,
        )
        state = self.engine.view()["pairs"]["gold"]
        self.assertEqual(D(state["spot"]), D(".04"))
        self.assertEqual(D(state["short"]), 0)
        self.assertFalse(state["exit_requested"])
        self.assertTrue(state["entry_paused"])


if __name__ == "__main__":
    unittest.main()
