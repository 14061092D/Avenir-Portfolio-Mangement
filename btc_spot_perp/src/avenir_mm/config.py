"""Strict configuration boundary for the BTC spot/perpetual pilot."""
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from typing import get_type_hints


@dataclass(frozen=True)
class Config:
    mode: str = 'shadow'
    spot_coin: str = '@142'
    perp_coin: str = 'BTC'
    capital_usdc: Decimal = Decimal('500')
    order_notional_usdc: Decimal = Decimal('20')
    max_inventory_usdc: Decimal = Decimal('100')
    min_notional_usdc: Decimal = Decimal('10')
    entry_offset_bp: Decimal = Decimal('100')
    minimum_profit_bp: Decimal = Decimal('50')
    execution_reserve_bp: Decimal = Decimal('5')
    spot_maker_fee: Decimal = Decimal('0.0004')
    perp_taker_fee: Decimal = Decimal('0.00045')
    slippage_bp: Decimal = Decimal('10')
    max_book_age_ms: int = 2000
    quote_min_life_ms: int = 60000
    reprice_bp: Decimal = Decimal('5')
    safety_cancel_edge_bp: Decimal = Decimal('80')
    dust_wait_ms: int = 10000
    max_unhedged_usdc: Decimal = Decimal('25')
    max_holding_ms: int = 3600000
    daily_loss_usdc: Decimal = Decimal('5')
    overall_drawdown_usdc: Decimal = Decimal('10')
    max_hedge_rejections: int = 3

    def __post_init__(self):
        if self.mode not in ('shadow', 'testnet', 'live'):
            raise ValueError('mode must be shadow, testnet or live')
        if self.perp_coin != 'BTC':
            raise ValueError('This pilot supports only BTC perpetuals')
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, Decimal) and (not v.is_finite() or v < 0):
                raise ValueError(f'{f.name} must be a finite non-negative Decimal')
        if not (0 < self.min_notional_usdc <= self.order_notional_usdc <= self.max_inventory_usdc <= self.capital_usdc):
            raise ValueError('Require 0 < minimum <= order <= inventory <= capital')
        if self.capital_usdc <= self.max_inventory_usdc:
            raise ValueError('Reserve capital for the hedge and unexpected losses')
        if not (0 < self.safety_cancel_edge_bp < self.entry_offset_bp < 10000):
            raise ValueError('Require 0 < safety edge < entry offset < 10000 bp')
        if not (0 < self.minimum_profit_bp < 10000 and self.slippage_bp < 10000):
            raise ValueError('Invalid profit/slippage basis points')
        if not (0 <= self.spot_maker_fee < 1 and 0 <= self.perp_taker_fee < 1):
            raise ValueError('Fees must be fractions below one')
        for name in ('max_book_age_ms','quote_min_life_ms','dust_wait_ms','max_holding_ms','max_hedge_rejections'):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if self.daily_loss_usdc <= 0 or self.overall_drawdown_usdc < self.daily_loss_usdc:
            raise ValueError('Loss limits must be positive and overall >= daily')
        if not (self.order_notional_usdc <= self.max_unhedged_usdc <= self.max_inventory_usdc):
            raise ValueError('Invalid unhedged exposure limit')

    @classmethod
    def from_dict(cls, data: dict) -> 'Config':
        allowed = {f.name for f in fields(cls)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f'Unknown configuration keys: {sorted(unknown)}')
        hints = get_type_hints(cls)
        values = {k: Decimal(str(v)) if hints[k] is Decimal else v for k,v in data.items()}
        return cls(**values)

    def to_dict(self) -> dict:
        return {k:str(v) if isinstance(v,Decimal) else v for k,v in asdict(self).items()}
