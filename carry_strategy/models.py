"""Normalized trading units and strategy configuration at adapter boundaries."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Optional

ZERO = Decimal("0")
BPS = Decimal("10000")


def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("numeric values must be finite")
    return result


def floor_step(value, step):
    if step <= ZERO:
        raise ValueError("quantity step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class Instrument:
    venue: str
    symbol: str
    kind: str
    underlying: str
    quote: str
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    contract_size: Decimal = Decimal("1")
    active: bool = True

    def __post_init__(self):
        if not isinstance(self.active, bool):
            raise ValueError("active must be a boolean")
        if self.kind not in ("spot", "linear"):
            raise ValueError("Only spot and linear contracts are supported")
        for name in ("tick_size", "qty_step", "contract_size"):
            if dec(getattr(self, name)) <= ZERO:
                raise ValueError(f"{name} must be positive")
        for name in ("min_qty", "min_notional"):
            if dec(getattr(self, name)) < ZERO:
                raise ValueError(f"{name} must be nonnegative")
        if not self.venue or not self.symbol or not self.underlying or not self.quote:
            raise ValueError("Instrument identity required")

    @property
    def base_step(self):
        return self.qty_step * self.contract_size

    @property
    def min_base_qty(self):
        return self.min_qty * self.contract_size


@dataclass(frozen=True)
class Pair:
    pair_id: str
    maker: Instrument
    hedge: Instrument
    maker_fee_bps: Optional[Decimal]
    hedge_fee_bps: Optional[Decimal]
    fee_in_base: bool = True
    mapping_verified: bool = False
    corporate_actions_ready: bool = True
    target_notional: Decimal = Decimal("100000")
    hedge_margin_fraction: Decimal = Decimal("0.5")
    maker_route: Optional[str] = None

    def __post_init__(self):
        for name in ("fee_in_base", "mapping_verified", "corporate_actions_ready"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.maker.kind != "spot" or self.hedge.kind != "linear":
            raise ValueError(
                "This strategy requires spot maker and linear perpetual hedge"
            )
        if (
            self.maker.underlying != self.hedge.underlying
            or self.maker.quote != self.hedge.quote
        ):
            raise ValueError("Underlying and quote must match explicitly")
        if self.maker.quote != "USDT":
            raise ValueError("USDT accounting required")
        if (
            self.target_notional <= ZERO
            or not ZERO < self.hedge_margin_fraction <= Decimal("1")
        ):
            raise ValueError("Invalid target or margin fraction")
        for fee in (self.maker_fee_bps, self.hedge_fee_bps):
            if fee is not None and (dec(fee) < ZERO or fee >= BPS):
                raise ValueError("Fee must be in[0,10000)bps")


@dataclass(frozen=True)
class Book:
    bids: tuple
    asks: tuple
    received_ms: int


@dataclass(frozen=True)
class BasisSample:
    timestamp_ms: int
    basis_bps: Decimal


@dataclass(frozen=True)
class FundingPoint:
    timestamp_ms: int
    rate: Decimal
    interval_ms: int
    kind: str = "regular"


@dataclass(frozen=True)
class SignalConfig:
    slice_notional: Decimal = Decimal("500")
    fair_window: int = 24
    deviation_window: int = 720
    entry_quantile: Decimal = Decimal("0.95")
    horizon_days: Decimal = Decimal("30")
    min_net_edge_bps: Decimal = Decimal("2")
    execution_reserve_bps: Decimal = Decimal("4")
    min_capital_apr: Decimal = Decimal("0")
    max_book_age_ms: int = 2000
    max_funding_age_ms: int = 32400000
    max_history_age_ms: int = 7200000


@dataclass(frozen=True)
class Market:
    pair_id: str
    maker_book: Book
    hedge_book: Book
    basis_history: tuple
    funding: tuple


@dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    maker_price: Decimal = ZERO
    maker_qty_base: Decimal = ZERO
    hedge_qty_base: Decimal = ZERO
    hedge_vwap: Decimal = ZERO
    entry_basis_bps: Decimal = ZERO
    deviation_bps: Decimal = ZERO
    threshold_bps: Decimal = ZERO
    funding_per_day: Decimal = ZERO
    expected_profit: Decimal = ZERO
    capital_required: Decimal = ZERO
    score: Decimal = ZERO


@dataclass(frozen=True)
class EngineConfig:
    capital_limit: Decimal = Decimal("150000")
    max_active_pairs: int = 3
    max_unhedged_notional: Decimal = Decimal("100")
    max_balance_age_ms: int = 30000
    maker_ttl_ms: int = 30000
    order_timeout_ms: int = 5000
    hedge_retry_ms: int = 1000
    hedge_slippage_bps: Decimal = Decimal("10")
    negative_carry_confirm_ms: int = 86400000
    max_hold_days: Optional[int] = None
    signal: SignalConfig = SignalConfig()

    def __post_init__(self):
        if dec(self.capital_limit) <= ZERO:
            raise ValueError("capital_limit must be positive")
        for name in (
            "max_active_pairs",
            "max_balance_age_ms",
            "maker_ttl_ms",
            "order_timeout_ms",
            "hedge_retry_ms",
            "negative_carry_confirm_ms",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("max_unhedged_notional", "hedge_slippage_bps"):
            if dec(getattr(self, name)) < ZERO:
                raise ValueError(f"{name} must be nonnegative")
        if self.max_hold_days is not None and (
            isinstance(self.max_hold_days, bool)
            or not isinstance(self.max_hold_days, int)
            or self.max_hold_days <= 0
        ):
            raise ValueError("max_hold_days must be a positive integer or null")
