"""Pure carry opportunity evaluation in normalized base units and USDT.

Funding estimates and the median exit basis are explicit historical estimates.
Order-book depth is checked for the proposed hedge quantity. The calculation
reserves fees and execution costs for the full holding cycle.
"""

from dataclasses import replace
from decimal import Decimal, ROUND_CEILING
from statistics import median

from .models import BPS, ZERO, Book, Decision, dec, floor_step

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
TIMESTAMP_TOLERANCE_MS = 1000
ONE = Decimal("1")


def validate_config(config):
    positive = ("slice_notional", "horizon_days")
    nonnegative = ("min_net_edge_bps", "execution_reserve_bps", "min_capital_apr")
    for field in positive:
        if dec(getattr(config, field)) <= ZERO:
            raise ValueError(f"{field} must be positive")
    for field in nonnegative:
        if dec(getattr(config, field)) < ZERO:
            raise ValueError(f"{field} must be nonnegative")
    for field in (
        "fair_window",
        "deviation_window",
        "max_book_age_ms",
        "max_funding_age_ms",
        "max_history_age_ms",
    ):
        value = getattr(config, field)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{field} must be a positive integer")
    if not ZERO < dec(config.entry_quantile) < ONE:
        raise ValueError("entry_quantile must be between zero and one")


def _valid_levels(levels, descending):
    if not levels:
        return False
    previous = None
    try:
        for price, quantity in levels:
            price, quantity = dec(price), dec(quantity)
            if price <= ZERO or quantity <= ZERO:
                return False
            if previous is not None:
                if descending and price >= previous:
                    return False
                if not descending and price <= previous:
                    return False
            previous = price
    except (ValueError, TypeError, ArithmeticError):
        return False
    return True


def validate_book(book: Book, now_ms: int, max_age_ms: int):
    """Return a rejection reason, or None for a valid fresh book."""
    if not isinstance(book.received_ms, int) or isinstance(book.received_ms, bool):
        return "book_timestamp_invalid"
    if book.received_ms > now_ms:
        return "book_from_future"
    if now_ms - book.received_ms > max_age_ms:
        return "book_stale"
    if not _valid_levels(book.bids, True) or not _valid_levels(book.asks, False):
        return "book_depth_invalid"
    if dec(book.bids[0][0]) >= dec(book.asks[0][0]):
        return "book_crossed_or_locked"
    return None


def book_vwap(book: Book, side: str, qty: Decimal):
    """Return executable VWAP for a base quantity, or None if depth is unusable."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    qty = dec(qty)
    if qty <= ZERO:
        raise ValueError("qty must be positive")
    levels = book.asks if side == "BUY" else book.bids
    if not _valid_levels(levels, side == "SELL"):
        return None
    remaining, notional = qty, ZERO
    for price, available in levels:
        quantity = min(remaining, dec(available))
        notional += quantity * dec(price)
        remaining -= quantity
        if remaining == ZERO:
            return notional / qty
    return None


def forecast_funding(points, now_ms: int, max_age_ms: int):
    """Estimate daily short-side carry using the lower of seven/thirty-day means.

    Regular intervals must cover both windows without unexplained gaps. An
    interval crossing a window boundary is prorated for this rate estimate.
    Negative special payments are retained conservatively, while positive
    one-off credits do not increase the estimate.
    """
    regular, special = [], []
    seen = set()
    try:
        for point in points:
            if (
                not isinstance(point.timestamp_ms, int)
                or isinstance(point.timestamp_ms, bool)
                or point.timestamp_ms > now_ms
            ):
                return None, "funding_timestamp_invalid"
            rate = dec(point.rate)
            key = (point.timestamp_ms, point.kind)
            if key in seen:
                return None, "funding_duplicate"
            seen.add(key)
            if point.kind == "regular":
                if (
                    not isinstance(point.interval_ms, int)
                    or isinstance(point.interval_ms, bool)
                    or point.interval_ms <= 0
                ):
                    return None, "funding_interval_invalid"
                regular.append(point)
            elif point.kind == "special":
                special.append((point.timestamp_ms, rate))
            else:
                return None, "funding_kind_unknown"
    except (ValueError, TypeError, ArithmeticError):
        return None, "funding_rate_invalid"
    if not regular:
        return None, "funding_history_missing"
    regular.sort(key=lambda point: point.timestamp_ms)
    end = regular[-1].timestamp_ms
    if now_ms - end > max_age_ms:
        return None, "funding_stale"
    estimates = []
    for days in (7, 30):
        start, cursor = end - days * DAY_MS, end - days * DAY_MS
        total = ZERO
        for point in regular:
            if point.timestamp_ms <= start:
                continue
            interval_start = point.timestamp_ms - point.interval_ms
            if cursor == start and interval_start <= start:
                overlap_start = start
            else:
                if abs(interval_start - cursor) > TIMESTAMP_TOLERANCE_MS:
                    return None, "funding_history_gap_or_overlap"
                overlap_start = cursor
            if interval_start > cursor + TIMESTAMP_TOLERANCE_MS:
                return None, "funding_history_incomplete"
            total += (
                dec(point.rate)
                * Decimal(point.timestamp_ms - overlap_start)
                / Decimal(point.interval_ms)
            )
            cursor = point.timestamp_ms
        if cursor != end:
            return None, "funding_history_incomplete"
        total += sum(
            (
                min(rate, ZERO)
                for timestamp, rate in special
                if start < timestamp <= end
            ),
            ZERO,
        )
        estimates.append(total / Decimal(days))
    return min(estimates), "ok"


def _basis_statistics(history, config, now_ms):
    needed = config.fair_window + config.deviation_window
    if len(history) < needed:
        return None, "basis_history_incomplete"
    samples = history[-needed:]
    previous = None
    values = []
    try:
        for sample in samples:
            timestamp = sample.timestamp_ms
            if (
                not isinstance(timestamp, int)
                or isinstance(timestamp, bool)
                or timestamp >= now_ms
            ):
                return None, "basis_history_not_prior"
            if (
                previous is not None
                and abs(timestamp - previous - HOUR_MS) > TIMESTAMP_TOLERANCE_MS
            ):
                return None, "basis_history_gap_or_order"
            basis = dec(sample.basis_bps)
            if basis <= -BPS:
                return None, "basis_history_invalid"
            values.append(basis)
            previous = timestamp
    except (ValueError, TypeError, ArithmeticError):
        return None, "basis_history_invalid"
    if now_ms - previous > config.max_history_age_ms:
        return None, "basis_history_stale"
    deviations = [
        values[index] - median(values[index - config.fair_window : index])
        for index in range(config.fair_window, len(values))
    ]
    rank = (
        int(
            (config.entry_quantile * len(deviations)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        - 1
    )
    threshold = sorted(deviations)[rank]
    return (median(values[-config.fair_window :]), threshold), "ok"


def evaluate(
    pair,
    market,
    config,
    now_ms: int,
    remaining_notional: Decimal,
    capital_available: Decimal,
    require_percentile: bool = True,
):
    """Evaluate one passive spot buy and its normalized linear-perpetual hedge.

    ``score`` and ``min_capital_apr`` are annualized fractions, so 0.05 means 5%.
    ``execution_reserve_bps`` is the total reserve for the whole cycle.
    """
    validate_config(config)
    if not isinstance(now_ms, int) or isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("now_ms must be a nonnegative integer")
    remaining_notional, capital_available = dec(remaining_notional), dec(
        capital_available
    )
    if market.pair_id != pair.pair_id:
        return Decision(False, "pair_market_mismatch")
    if not pair.mapping_verified:
        return Decision(False, "mapping_unverified")
    if not pair.corporate_actions_ready:
        return Decision(False, "corporate_actions_unresolved")
    if not pair.maker.active or not pair.hedge.active:
        return Decision(False, "instrument_inactive")
    if pair.maker_fee_bps is None or pair.hedge_fee_bps is None:
        return Decision(False, "account_fees_missing")
    if remaining_notional <= ZERO:
        return Decision(False, "target_reached")
    if capital_available <= ZERO:
        return Decision(False, "capital_unavailable")
    for label, book in (("maker", market.maker_book), ("hedge", market.hedge_book)):
        reason = validate_book(book, now_ms, config.max_book_age_ms)
        if reason is not None:
            return Decision(False, label + "_" + reason)
    funding, reason = forecast_funding(
        market.funding, now_ms, config.max_funding_age_ms
    )
    if funding is None:
        return Decision(False, reason)
    decision = Decision(False, "evaluating", funding_per_day=funding)
    if funding <= ZERO:
        return replace(decision, reason="nonpositive_carry")
    statistics, reason = _basis_statistics(market.basis_history, config, now_ms)
    if statistics is None:
        return replace(decision, reason=reason)
    fair_basis, threshold = statistics
    maker_price = floor_step(dec(market.maker_book.bids[0][0]), pair.maker.tick_size)
    if maker_price <= ZERO:
        return replace(decision, reason="maker_price_invalid")
    maker_fee, hedge_fee = pair.maker_fee_bps / BPS, pair.hedge_fee_bps / BPS
    net_factor = ONE - maker_fee if pair.fee_in_base else ONE
    cash_factor = ONE if pair.fee_in_base else ONE + maker_fee
    hedge_ask = dec(market.hedge_book.asks[0][0])
    capital_per_gross_base = maker_price * cash_factor + net_factor * hedge_ask * (
        pair.hedge_margin_fraction + hedge_fee
    )
    budget = min(config.slice_notional, remaining_notional, pair.target_notional)
    gross_qty = floor_step(
        min(budget / maker_price, capital_available / capital_per_gross_base),
        pair.maker.base_step,
    )
    net_spot_qty = gross_qty * net_factor
    hedge_qty = floor_step(net_spot_qty, pair.hedge.base_step)
    spot_notional = gross_qty * maker_price
    if (
        gross_qty < pair.maker.min_base_qty
        or spot_notional < pair.maker.min_notional
        or hedge_qty < pair.hedge.min_base_qty
    ):
        return replace(decision, reason="below_minimum_size")
    if gross_qty <= ZERO or hedge_qty <= ZERO:
        return replace(decision, reason="below_minimum_size")
    vwap = book_vwap(market.hedge_book, "SELL", hedge_qty)
    if vwap is None:
        return replace(decision, reason="insufficient_hedge_depth")
    hedge_notional = hedge_qty * vwap
    if hedge_notional < pair.hedge.min_notional:
        return replace(decision, reason="below_hedge_minimum_notional")
    entry_basis = (vwap / maker_price - ONE) * BPS
    deviation = entry_basis - fair_basis
    expected_exit_perp = maker_price * (ONE + fair_basis / BPS)
    fees = (
        spot_notional * maker_fee
        + hedge_notional * hedge_fee
        + net_spot_qty * maker_price * maker_fee
        + hedge_qty * expected_exit_perp * hedge_fee
    )
    execution_reserve = spot_notional * config.execution_reserve_bps / BPS
    basis_profit = hedge_qty * (vwap - expected_exit_perp)
    funding_profit = hedge_notional * funding * config.horizon_days
    profit = basis_profit + funding_profit - fees - execution_reserve
    capital = (
        spot_notional * cash_factor
        + hedge_qty * hedge_ask * pair.hedge_margin_fraction
        + hedge_notional * hedge_fee
    )
    score = profit / capital * Decimal("365") / config.horizon_days
    decision = replace(
        decision,
        maker_price=maker_price,
        maker_qty_base=gross_qty,
        hedge_qty_base=hedge_qty,
        hedge_vwap=vwap,
        entry_basis_bps=entry_basis,
        deviation_bps=deviation,
        threshold_bps=threshold,
        expected_profit=profit,
        capital_required=capital,
        score=score,
    )
    if capital > capital_available:
        return replace(decision, reason="capital_unavailable")
    if require_percentile and deviation < threshold:
        return replace(decision, reason="below_entry_percentile")
    if profit / spot_notional * BPS < config.min_net_edge_bps:
        return replace(decision, reason="below_net_edge")
    if score < config.min_capital_apr:
        return replace(decision, reason="below_capital_return")
    return replace(decision, accepted=True, reason="entry_ready")
