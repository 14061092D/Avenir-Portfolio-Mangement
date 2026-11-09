"""Durable strategy state and order intents, consumed by a broker adapter.

All public methods are serialized through SQLite. Dispatchers persist a send
attempt before calling a venue, and resolve uncertain requests by client ID.
Order acknowledgements never change position inventory.
"""

from contextlib import contextmanager
from dataclasses import asdict, replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from .models import ZERO, BPS, EngineConfig, dec, floor_step
from .signal import evaluate, book_vwap, validate_book, forecast_funding

TERMINAL = {"filled", "canceled", "rejected"}


def encode(value):
    return json.dumps(
        value,
        default=lambda x: str(x) if isinstance(x, Decimal) else asdict(x),
        sort_keys=True,
    )


def pair_state():
    return dict(
        spot="0",
        short="0",
        spot_cost="0",
        short_average="0",
        margin="0",
        campaign=False,
        started_ms=None,
        negative_since=None,
        exit_requested=False,
        exit_reason="",
        ready=False,
        inventory_ok=False,
        entry_paused=False,
        pause_reason="reconciliation_required",
    )


class Engine:
    def __init__(self, db_path, pairs, config=None):
        self.config = config or EngineConfig()
        if self.config.capital_limit <= ZERO or self.config.max_active_pairs < 1:
            raise ValueError("Positive capital and active-pair limits required")
        self.pairs = {p.pair_id: p for p in pairs}
        if len(self.pairs) != len(pairs):
            raise ValueError("Duplicate pair IDs")
        self.markets = {}
        self.db = sqlite3.connect(str(db_path), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(venue TEXT,execution_id TEXT,payload TEXT NOT NULL,PRIMARY KEY(venue,execution_id));
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL);
        """
        )
        with self._transaction() as state:
            for key in state["pairs"]:
                if key not in self.pairs:
                    raise ValueError("Configuration must retain every persisted pair")
            for key, pair in self.pairs.items():
                identity = encode((pair.maker, pair.hedge, pair.fee_in_base))
                if key in state["identities"] and state["identities"][key] != identity:
                    raise ValueError(
                        "Instrument mapping changed; reconcile/migrate explicitly"
                    )
                state["identities"][key] = identity
                state["pairs"].setdefault(key, pair_state())
                state["pairs"][key]["ready"] = False
                state["pairs"][key]["inventory_ok"] = False
                state["pairs"][key]["pause_reason"] = "reconciliation_required"
                for order in self._orders(state, key, "maker"):
                    self._cancel(state, order, 0)
            self._request_reconcile(state, 0, "startup")

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
            state = (
                json.loads(row[0])
                if row
                else dict(
                    seq=0,
                    pairs={},
                    identities={},
                    orders={},
                    balances={},
                    balance_ms=-1,
                    last_fill_ms=-1,
                )
            )
            yield state
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES(1,?)", (encode(state),)
            )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def view(self):
        row = self.db.execute("SELECT payload FROM state WHERE id=1").fetchone()
        return json.loads(row[0])

    def pending_commands(self):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM commands WHERE status='queued' ORDER BY rowid"
            )
        ]

    def mark_dispatched(self, command_id, now_ms):
        """Call BEFORE network send. Never resend an uncertain place automatically."""
        with self._transaction() as state:
            row = self.db.execute(
                "SELECT payload,status FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            if row is None or row[1] != "queued":
                return False
            command = json.loads(row[0])
            if command["kind"] == "place":
                order = state["orders"][command["client_order_id"]]
                position = state["pairs"][order["pair_id"]]
                market = self.markets.get(order["pair_id"])
                can_dispatch = (
                    position["inventory_ok"]
                    if order["leg"] == "hedge"
                    else position["ready"]
                )
                if not can_dispatch or market is None:
                    return False
                book = (
                    market.maker_book if order["leg"] == "maker" else market.hedge_book
                )
                if validate_book(book, now_ms, self.config.signal.max_book_age_ms):
                    return False
                if order["cancel_requested"] or not self._active(order):
                    return False
            self.db.execute(
                "UPDATE commands SET status='dispatched' WHERE id=?", (command_id,)
            )
            if command["kind"] == "place":
                order = state["orders"][command["client_order_id"]]
                order["status"] = "submitted"
                order["updated_ms"] = now_ms
            return True

    def _emit(self, state, payload):
        state["seq"] += 1
        command_id = f'carry-{state["seq"]:012d}'
        payload = dict(command_id=command_id, **payload)
        self.db.execute(
            "INSERT INTO commands VALUES(?,?,?)",
            (command_id, encode(payload), "queued"),
        )
        return command_id

    def _request_reconcile(self, state, now_ms, reason):
        for payload, status in self.db.execute(
            "SELECT payload,status FROM commands WHERE status!='done'"
        ):
            if json.loads(payload)["kind"] == "reconcile":
                return
        self._emit(state, dict(kind="reconcile", time_ms=now_ms, reason=reason))

    @staticmethod
    def _active(order):
        return order["status"] not in TERMINAL or dec(order["reported_filled"]) > dec(
            order["filled"]
        )

    def _orders(self, state, pair_id, leg=None):
        return [
            o
            for o in state["orders"].values()
            if o["pair_id"] == pair_id
            and self._active(o)
            and (leg is None or o["leg"] == leg)
        ]

    def _cancel(self, state, order, now_ms):
        if order["cancel_requested"] or order["status"] in TERMINAL:
            return
        if order["status"] == "pending_submit":
            for cid, payload, status in self.db.execute(
                "SELECT id,payload,status FROM commands WHERE status='queued'"
            ):
                command = json.loads(payload)
                if (
                    command["kind"] == "place"
                    and command["client_order_id"] == order["id"]
                ):
                    self.db.execute(
                        "UPDATE commands SET status='done' WHERE id=?", (cid,)
                    )
                    order["status"] = "canceled"
                    order["reported_filled"] = order["filled"]
                    return
        order["cancel_requested"] = True
        self._emit(
            state,
            dict(
                kind="cancel",
                client_order_id=order["id"],
                venue=order["venue"],
                symbol=order["symbol"],
                time_ms=now_ms,
            ),
        )

    def _new_order(
        self, state, pair, leg, side, qty, price, now_ms, capital_reserve=ZERO
    ):
        instrument = pair.maker if leg == "maker" else pair.hedge
        state["seq"] += 1
        client_id = f'carry-order-{state["seq"]:012d}'
        order = dict(
            id=client_id,
            pair_id=pair.pair_id,
            leg=leg,
            side=side,
            qty=str(qty),
            filled="0",
            reported_filled="0",
            price=str(price),
            venue=instrument.venue,
            symbol=instrument.symbol,
            status="pending_submit",
            created_ms=now_ms,
            updated_ms=now_ms,
            cancel_requested=False,
            capital_reserve=str(capital_reserve),
        )
        maker_cash = (
            qty
            * price
            * (1 + (pair.maker_fee_bps or ZERO) / BPS if not pair.fee_in_base else 1)
        )
        order["maker_cash_reserve"] = str(maker_cash)
        order["hedge_cash_reserve"] = str(max(ZERO, capital_reserve - maker_cash))
        state["orders"][client_id] = order
        self._emit(
            state,
            dict(
                kind="place",
                client_order_id=client_id,
                pair_id=pair.pair_id,
                venue=instrument.venue,
                symbol=instrument.symbol,
                side=side,
                qty_base=qty,
                qty=qty / instrument.contract_size,
                price=price,
                time_in_force="PostOnly" if leg == "maker" else "IOC",
                reduce_only=leg == "hedge" and side == "BUY",
                route=pair.maker_route if leg == "maker" else None,
                time_ms=now_ms,
            ),
        )
        return client_id

    def _reserved(self, state):
        used = ZERO
        for key, position in state["pairs"].items():
            used += (
                dec(position["spot_cost"])
                + dec(position["margin"])
                + self._pending_margin(state, key)
            )
        for order in state["orders"].values():
            if (
                self._active(order)
                and order["leg"] == "maker"
                and order["side"] == "BUY"
            ):
                remaining = max(ZERO, dec(order["qty"]) - dec(order["filled"])) / dec(
                    order["qty"]
                )
                used += dec(order["capital_reserve"]) * remaining
        return used

    def _pending_margin(self, state, key):
        position = state["pairs"][key]
        pair = self.pairs[key]
        qty = max(ZERO, dec(position["spot"]) - dec(position["short"]))
        market = self.markets.get(key)
        price = (
            market.hedge_book.asks[0][0]
            if market and market.hedge_book.asks
            else (
                dec(position["spot_cost"]) / dec(position["spot"])
                if dec(position["spot"])
                else ZERO
            )
        )
        return (
            qty
            * price
            * (1 + self.config.hedge_slippage_bps / BPS)
            * (pair.hedge_margin_fraction + (pair.hedge_fee_bps or ZERO) / BPS)
        )

    def _free_cash(self, state, venue):
        free = dec(state["balances"].get(venue, "0"))
        for key, pair in self.pairs.items():
            if pair.hedge.venue == venue:
                free -= self._pending_margin(state, key)
        for order in state["orders"].values():
            if (
                not self._active(order)
                or order["leg"] != "maker"
                or order["side"] != "BUY"
            ):
                continue
            pair = self.pairs[order["pair_id"]]
            unfilled = max(ZERO, dec(order["qty"]) - dec(order["filled"]))
            fraction = unfilled / dec(order["qty"])
            if venue == pair.maker.venue:
                free -= dec(order["maker_cash_reserve"]) * fraction
            if venue == pair.hedge.venue:
                free -= dec(order["hedge_cash_reserve"]) * fraction
        return max(ZERO, free)

    def _cash(self, state, venue, change):
        state["balances"][venue] = str(dec(state["balances"].get(venue, "0")) + change)

    def on_market(self, market, now_ms):
        if market.pair_id not in self.pairs:
            raise ValueError("Unknown pair")
        self.markets[market.pair_id] = market
        with self._transaction() as state:
            self._manage(state, market.pair_id, now_ms)
            self._allocate(state, now_ms)
        return self.pending_commands()

    def tick(self, now_ms):
        with self._transaction() as state:
            for key in self.pairs:
                self._manage(state, key, now_ms)
            self._allocate(state, now_ms)
        return self.pending_commands()

    def request_exit(self, pair_id, now_ms, reason="operator_exit"):
        with self._transaction() as state:
            position = state["pairs"][pair_id]
            position["exit_requested"] = True
            position["exit_reason"] = reason
            position["entry_paused"] = True
            self._manage(state, pair_id, now_ms)
        return self.pending_commands()

    def resume_pair(self, pair_id):
        with self._transaction() as state:
            state["pairs"][pair_id]["entry_paused"] = False

    def _manage(self, state, key, now_ms):
        pair = self.pairs[key]
        position = state["pairs"][key]
        market = self.markets.get(key)
        for order in self._orders(state, key):
            if now_ms - order["updated_ms"] > self.config.order_timeout_ms and order[
                "status"
            ] in ("submitted", "pending_submit"):
                position["ready"] = False
                position["pause_reason"] = "order_outcome_unknown"
                self._request_reconcile(state, now_ms, "order_outcome_unknown")
            if (
                order["leg"] == "maker"
                and now_ms - order["created_ms"] >= self.config.maker_ttl_ms
            ):
                self._cancel(state, order, now_ms)
        if market is None:
            return
        if not position["inventory_ok"]:
            for order in self._orders(state, key, "maker"):
                self._cancel(state, order, now_ms)
            return
        stale = validate_book(
            market.maker_book, now_ms, self.config.signal.max_book_age_ms
        )
        hedge_stale = validate_book(
            market.hedge_book, now_ms, self.config.signal.max_book_age_ms
        )
        if stale or hedge_stale:
            for order in self._orders(state, key, "maker"):
                self._cancel(state, order, now_ms)
            return
        delta = dec(position["spot"]) - dec(position["short"])
        hedges = self._orders(state, key, "hedge")
        if hedges:
            for order in self._orders(state, key, "maker"):
                self._cancel(state, order, now_ms)
            return
        target_short = floor_step(dec(position["spot"]), pair.hedge.base_step)
        if position["exit_requested"]:
            spot_qty = dec(position["spot"])
            spot_value = spot_qty * market.maker_book.asks[0][0]
            below_minimum = (
                spot_qty < pair.maker.min_base_qty
                or spot_value < pair.maker.min_notional
            )
            if below_minimum and spot_value <= self.config.max_unhedged_notional:
                target_short = ZERO
        hedge_delta = target_short - dec(position["short"])
        qty = floor_step(abs(hedge_delta), pair.hedge.base_step)
        side = "SELL" if hedge_delta > ZERO else "BUY"
        if qty >= pair.hedge.min_base_qty and qty > ZERO:
            for order in self._orders(state, key, "maker"):
                self._cancel(state, order, now_ms)
            if (
                now_ms - position.get("last_hedge_ms", -self.config.hedge_retry_ms)
                < self.config.hedge_retry_ms
            ):
                return
            best = (
                market.hedge_book.bids[0][0]
                if side == "SELL"
                else market.hedge_book.asks[0][0]
            )
            band = self.config.hedge_slippage_bps / BPS
            limit = (
                floor_step(best * (Decimal("1") - band), pair.hedge.tick_size)
                if side == "SELL"
                else (
                    floor_step(best * (Decimal("1") + band), pair.hedge.tick_size)
                    + pair.hedge.tick_size
                )
            )
            levels = (
                market.hedge_book.bids if side == "SELL" else market.hedge_book.asks
            )
            available = sum(
                (
                    size
                    for price, size in levels
                    if (price >= limit if side == "SELL" else price <= limit)
                ),
                ZERO,
            )
            qty = min(qty, floor_step(available, pair.hedge.base_step))
            if (
                qty < pair.hedge.min_base_qty
                or qty <= ZERO
                or (side == "SELL" and qty * limit < pair.hedge.min_notional)
            ):
                position["pause_reason"] = "hedge_depth_outside_limit"
                return
            if side == "SELL":
                needed = (
                    qty
                    * limit
                    * (pair.hedge_margin_fraction + (pair.hedge_fee_bps or ZERO) / BPS)
                )
                if (
                    self._free_cash(state, pair.hedge.venue)
                    + self._pending_margin(state, key)
                    < needed
                ):
                    position["pause_reason"] = "hedge_cash_insufficient"
                    self._request_reconcile(state, now_ms, "hedge_cash_insufficient")
                    return
            self._new_order(state, pair, "hedge", side, qty, limit, now_ms)
            position["last_hedge_ms"] = now_ms
            return
        if (
            abs(delta) * market.maker_book.bids[0][0]
            > self.config.max_unhedged_notional
        ):
            position["pause_reason"] = "unhedgeable_exposure"
            for order in self._orders(state, key, "maker"):
                self._cancel(state, order, now_ms)
            return
        funding, reason = forecast_funding(
            market.funding, now_ms, self.config.signal.max_funding_age_ms
        )
        if funding is not None and funding < ZERO and dec(position["spot"]) > ZERO:
            if (
                position["negative_since"] is None
                or now_ms - position.get("negative_last_ms", 0)
                > self.config.signal.max_funding_age_ms
            ):
                position["negative_since"] = now_ms
            position["negative_last_ms"] = now_ms
            if (
                now_ms - position["negative_since"]
                >= self.config.negative_carry_confirm_ms
            ):
                position["exit_requested"] = True
                position["exit_reason"] = "persistent_negative_carry"
        else:
            position["negative_since"] = None
            position["negative_last_ms"] = None
        if self.config.max_hold_days is not None and position["started_ms"] is not None:
            if now_ms - position["started_ms"] >= self.config.max_hold_days * 86400000:
                position["exit_requested"] = True
                position["exit_reason"] = "holding_limit"
        if position["exit_requested"]:
            makers = self._orders(state, key, "maker")
            for order in makers:
                if order["side"] == "BUY":
                    self._cancel(state, order, now_ms)
            if makers:
                return
            price = market.maker_book.asks[0][0]
            qty = floor_step(
                min(dec(position["spot"]), self.config.signal.slice_notional / price),
                pair.maker.base_step,
            )
            if (
                qty >= pair.maker.min_base_qty
                and qty * price >= pair.maker.min_notional
            ):
                self._new_order(state, pair, "maker", "SELL", qty, price, now_ms)
            elif dec(position["short"]) == ZERO:
                position["campaign"] = False
                position["started_ms"] = None
                position["exit_requested"] = False
                position["exit_reason"] = ""
            return
        # Re-evaluate resting entries on every fresh book, including economics after first P95.
        for order in self._orders(state, key, "maker"):
            if order["side"] != "BUY":
                continue
            decision = evaluate(
                pair,
                market,
                self.config.signal,
                now_ms,
                remaining_notional=pair.target_notional - dec(position["spot_cost"]),
                capital_available=self.config.capital_limit,
                require_percentile=not position["campaign"],
            )
            if (
                not decision.accepted
                or decision.maker_price != dec(order["price"])
                or not position["ready"]
            ):
                self._cancel(state, order, now_ms)

    def _allocate(self, state, now_ms):
        if (
            state["balance_ms"] < 0
            or now_ms - state["balance_ms"] > self.config.max_balance_age_ms
        ):
            return
        candidates = []
        active_count = sum(
            p["campaign"] or dec(p["short"]) > ZERO or bool(self._orders(state, key))
            for key, p in state["pairs"].items()
        )
        for key, pair in self.pairs.items():
            position = state["pairs"][key]
            if (
                not position["ready"]
                or position["exit_requested"]
                or position.get("entry_paused")
                or self._orders(state, key)
            ):
                continue
            if (
                active_count >= self.config.max_active_pairs
                and not position["campaign"]
            ):
                continue
            market = self.markets.get(key)
            if market is None:
                continue
            delta = abs(dec(position["spot"]) - dec(position["short"]))
            if delta >= pair.hedge.base_step or dec(position["short"]) > dec(
                position["spot"]
            ):
                continue
            available = self.config.capital_limit - self._reserved(state)
            cash = self._free_cash(state, pair.maker.venue)
            hedge_cash = self._free_cash(state, pair.hedge.venue)
            if pair.maker.venue == pair.hedge.venue:
                available = min(available, cash)
            else:
                available = min(
                    available,
                    cash * (1 + pair.hedge_margin_fraction),
                    hedge_cash
                    / pair.hedge_margin_fraction
                    * (1 + pair.hedge_margin_fraction),
                )
            decision = evaluate(
                pair,
                market,
                self.config.signal,
                now_ms,
                remaining_notional=pair.target_notional - dec(position["spot_cost"]),
                capital_available=available,
                require_percentile=not position["campaign"],
            )
            if decision.accepted:
                net_new = decision.maker_qty_base * (
                    1 - (pair.maker_fee_bps or ZERO) / BPS if pair.fee_in_base else 1
                )
                needed = floor_step(
                    dec(position["spot"]) + net_new, pair.hedge.base_step
                ) - dec(position["short"])
                cumulative_vwap = (
                    book_vwap(market.hedge_book, "SELL", needed)
                    if needed > ZERO
                    else None
                )
                if cumulative_vwap is None:
                    position["last_entry_reason"] = (
                        "cumulative_hedge_depth_insufficient"
                    )
                    continue
                extra = max(ZERO, needed - decision.hedge_qty_base)
                extra_capital = (
                    extra
                    * market.hedge_book.asks[0][0]
                    * (pair.hedge_margin_fraction + (pair.hedge_fee_bps or ZERO) / BPS)
                )
                extra_cost = (
                    extra * cumulative_vwap * (pair.hedge_fee_bps or ZERO) / BPS * 2
                )
                extra_cost += max(ZERO, decision.hedge_vwap - cumulative_vwap) * needed
                capital = decision.capital_required + extra_capital
                profit = decision.expected_profit - extra_cost
                score = (
                    profit / capital * Decimal("365") / self.config.signal.horizon_days
                )
                if (
                    capital > available
                    or profit
                    < decision.maker_price
                    * decision.maker_qty_base
                    * self.config.signal.min_net_edge_bps
                    / BPS
                    or score < self.config.signal.min_capital_apr
                ):
                    position["last_entry_reason"] = "cumulative_hedge_cost_or_capital"
                    continue
                decision = replace(
                    decision,
                    hedge_qty_base=needed,
                    hedge_vwap=cumulative_vwap,
                    capital_required=capital,
                    expected_profit=profit,
                    score=score,
                )
                dust = (
                    decision.maker_qty_base
                    * (
                        1 - (pair.maker_fee_bps or ZERO) / BPS
                        if pair.fee_in_base
                        else 1
                    )
                    - decision.hedge_qty_base
                ) * decision.maker_price
                if dust <= self.config.max_unhedged_notional:
                    candidates.append((decision.score, key, decision))
            position["last_entry_reason"] = decision.reason
        for _, key, decision in sorted(candidates, key=lambda x: x[0], reverse=True):
            if decision.capital_required > self.config.capital_limit - self._reserved(
                state
            ):
                continue
            position = state["pairs"][key]
            pair = self.pairs[key]
            if (
                active_count >= self.config.max_active_pairs
                and not position["campaign"]
            ):
                continue
            # Recheck venue cash after reservations for higher-ranked opportunities.
            spend = decision.maker_price * decision.maker_qty_base
            required_maker = spend * (1 + (pair.maker_fee_bps or ZERO) / BPS)
            required_hedge = spend * pair.hedge_margin_fraction
            if pair.maker.venue == pair.hedge.venue:
                if (
                    self._free_cash(state, pair.maker.venue)
                    < required_maker + required_hedge
                ):
                    continue
            elif (
                self._free_cash(state, pair.maker.venue) < required_maker
                or self._free_cash(state, pair.hedge.venue) < required_hedge
            ):
                continue
            self._new_order(
                state,
                pair,
                "maker",
                "BUY",
                decision.maker_qty_base,
                decision.maker_price,
                now_ms,
                decision.capital_required,
            )
            if not position["campaign"]:
                active_count += 1

    def on_fill(
        self,
        venue,
        execution_id,
        client_order_id,
        qty_base,
        price,
        fee_amount,
        fee_currency,
        time_ms,
    ):
        qty, price, fee = dec(qty_base), dec(price), dec(fee_amount)
        if qty <= ZERO or price <= ZERO:
            raise ValueError("Positive execution quantity and price required")
        event = dict(
            venue=venue,
            execution_id=execution_id,
            client_order_id=client_order_id,
            qty_base=qty,
            price=price,
            fee_amount=fee,
            fee_currency=fee_currency,
            time_ms=time_ms,
        )
        with self._transaction() as state:
            prior = self.db.execute(
                "SELECT payload FROM fills WHERE venue=? AND execution_id=?",
                (venue, execution_id),
            ).fetchone()
            if prior:
                if prior[0] != encode(event):
                    raise ValueError("Conflicting duplicate execution")
                return False
            order = state["orders"].get(client_order_id)
            if order is None or order["venue"] != venue:
                raise ValueError("Unknown strategy order")
            filled = dec(order["filled"]) + qty
            if filled > dec(order["qty"]):
                raise ValueError("Execution exceeds original order quantity")
            pair = self.pairs[order["pair_id"]]
            position = state["pairs"][order["pair_id"]]
            base_fee = fee if fee_currency == "base" else ZERO
            quote_fee = fee if fee_currency == "quote" else ZERO
            if fee_currency not in ("base", "quote"):
                position["ready"] = False
                position["pause_reason"] = "unvalued_fee_currency"
                position["accounting_issue"] = "unvalued_fee_currency"
            if order["leg"] == "maker":
                old_spot = dec(position["spot"])
                if order["side"] == "BUY":
                    net = qty - base_fee
                    if net <= ZERO:
                        raise ValueError("Base fee exceeds fill")
                    position["spot"] = str(old_spot + net)
                    position["spot_cost"] = str(
                        dec(position["spot_cost"]) + qty * price + quote_fee
                    )
                    self._cash(state, venue, -qty * price - quote_fee)
                    position["campaign"] = True
                    position["started_ms"] = (
                        position["started_ms"]
                        if position["started_ms"] is not None
                        else time_ms
                    )
                else:
                    sold = qty + base_fee
                    if sold > old_spot:
                        raise ValueError("Spot sell exceeds owned inventory")
                    position["spot"] = str(old_spot - sold)
                    position["spot_cost"] = str(
                        dec(position["spot_cost"]) * (old_spot - sold) / old_spot
                    )
                    self._cash(state, venue, qty * price - quote_fee)
            else:
                if base_fee != ZERO:
                    position["ready"] = False
                    position["pause_reason"] = "unexpected_derivative_base_fee"
                    position["accounting_issue"] = "unexpected_derivative_base_fee"
                old_short = dec(position["short"])
                if order["side"] == "SELL":
                    position["short_average"] = str(
                        (dec(position["short_average"]) * old_short + qty * price)
                        / (old_short + qty)
                    )
                    position["short"] = str(old_short + qty)
                    margin = qty * price * pair.hedge_margin_fraction
                    position["margin"] = str(dec(position["margin"]) + margin)
                    self._cash(state, venue, -margin - quote_fee)
                else:
                    if qty > old_short:
                        raise ValueError("Reduce-only fill exceeds short inventory")
                    released = dec(position["margin"]) * qty / old_short
                    position["margin"] = str(dec(position["margin"]) - released)
                    position["short"] = str(old_short - qty)
                    self._cash(
                        state,
                        venue,
                        released
                        + qty * (dec(position["short_average"]) - price)
                        - quote_fee,
                    )
            order["filled"] = str(filled)
            if filled == dec(order["qty"]):
                order["status"] = "filled"
                order["reported_filled"] = str(filled)
            order["updated_ms"] = time_ms
            state["last_fill_ms"] = max(state["last_fill_ms"], time_ms)
            self.db.execute(
                "INSERT INTO fills VALUES(?,?,?)", (venue, execution_id, encode(event))
            )
            self._manage(state, order["pair_id"], time_ms)
        return True

    def on_order_update(self, client_order_id, status, cumulative_qty_base, now_ms):
        if status not in {
            "open",
            "partially_filled",
            "filled",
            "canceled",
            "rejected",
            "unknown",
        }:
            raise ValueError("Unknown order status")
        cumulative = dec(cumulative_qty_base)
        with self._transaction() as state:
            order = state["orders"][client_order_id]
            if cumulative < ZERO or cumulative > dec(order["qty"]):
                raise ValueError("Invalid cumulative fill quantity")
            if status == "filled" and cumulative != dec(order["qty"]):
                raise ValueError("Filled status requires full cumulative quantity")
            if cumulative < max(dec(order["filled"]), dec(order["reported_filled"])):
                return
            if order["status"] in TERMINAL and status not in TERMINAL:
                return
            order["status"] = status
            order["reported_filled"] = str(cumulative)
            order["updated_ms"] = now_ms
            if status == "unknown":
                state["pairs"][order["pair_id"]]["ready"] = False
                self._request_reconcile(state, now_ms, "unknown_order_status")
            self._manage(state, order["pair_id"], now_ms)

    def reconcile(self, positions, orders, available_quote, now_ms):
        """Broker supplies coherent dedicated-account inventory and known-order lookup.

        positions maps pair_id to spot/short base quantities. orders maps each
        unresolved client ID to status and cumulative_qty_base. Missing orders
        retain their reservations and block new entry.
        """
        with self._transaction() as state:
            newest_order = max(
                (o["updated_ms"] for o in state["orders"].values()), default=-1
            )
            if now_ms < max(state["last_fill_ms"], newest_order):
                return False
            all_ok = True
            unexpected = set(orders) - set(state["orders"])
            for key, pair in self.pairs.items():
                position = state["pairs"][key]
                actual = positions.get(key)
                ok = (
                    not unexpected
                    and not position.get("accounting_issue")
                    and actual is not None
                    and dec(actual["spot"]) == dec(position["spot"])
                    and dec(actual["short"]) == dec(position["short"])
                )
                inventory_ok = (
                    not unexpected
                    and actual is not None
                    and dec(actual["spot"]) == dec(position["spot"])
                    and dec(actual["short"]) == dec(position["short"])
                )
                for order in self._orders(state, key):
                    remote = orders.get(order["id"])
                    if remote is None:
                        ok = False
                        inventory_ok = False
                        continue
                    cumulative = dec(remote["cumulative_qty_base"])
                    if cumulative != dec(order["filled"]) or cumulative < dec(
                        order["reported_filled"]
                    ):
                        ok = False
                        inventory_ok = False
                        continue
                    if remote["status"] not in TERMINAL | {"open", "partially_filled"}:
                        ok = False
                        inventory_ok = False
                        continue
                    if remote["status"] == "filled" and cumulative != dec(order["qty"]):
                        ok = False
                        inventory_ok = False
                        continue
                    order["status"] = remote["status"]
                    order["reported_filled"] = str(cumulative)
                    order["updated_ms"] = now_ms
                    if order["cancel_requested"] and remote["status"] in (
                        "open",
                        "partially_filled",
                    ):
                        retryable = False
                        for cid, payload in self.db.execute(
                            "SELECT id,payload FROM commands WHERE status!='done'"
                        ):
                            command = json.loads(payload)
                            if (
                                command["kind"] == "cancel"
                                and command["client_order_id"] == order["id"]
                            ):
                                self.db.execute(
                                    "UPDATE commands SET status='queued' WHERE id=?",
                                    (cid,),
                                )
                                retryable = True
                        if not retryable:
                            order["cancel_requested"] = False
                            self._cancel(state, order, now_ms)
                position["ready"] = ok
                position["inventory_ok"] = inventory_ok
                position["pause_reason"] = "" if ok else "reconciliation_mismatch"
                if not ok:
                    all_ok = False
                    for order in self._orders(state, key, "maker"):
                        self._cancel(state, order, now_ms)
            if all_ok:
                venues = {
                    i.venue for p in self.pairs.values() for i in (p.maker, p.hedge)
                }
                if not venues.issubset(available_quote) or any(
                    dec(available_quote[v]) < ZERO for v in venues
                ):
                    raise ValueError(
                        "Complete nonnegative available cash snapshot required"
                    )
                state["balances"] = {v: str(dec(available_quote[v])) for v in venues}
                state["balance_ms"] = now_ms
                for cid, payload in self.db.execute(
                    "SELECT id,payload FROM commands WHERE status!='done'"
                ):
                    if json.loads(payload)["kind"] == "reconcile":
                        self.db.execute(
                            "UPDATE commands SET status='done' WHERE id=?", (cid,)
                        )
            else:
                self._request_reconcile(state, now_ms, "reconciliation_mismatch")
            return all_ok
