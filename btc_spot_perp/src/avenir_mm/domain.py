"""Venue-neutral data and exact decimal price/quantity arithmetic."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Literal

D = Decimal
ZERO = D('0')
Market = Literal['spot', 'perp']
Side = Literal['buy', 'sell']


@dataclass(frozen=True)
class Level:
    price: Decimal
    qty: Decimal


@dataclass(frozen=True)
class Book:
    coin: str
    time_ms: int
    received_ms: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    @property
    def bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def ask(self) -> Decimal:
        return self.asks[0].price

    def vwap(self, side: Side, qty: Decimal) -> Decimal | None:
        if qty <= ZERO:
            return None
        remaining, cost = qty, ZERO
        for level in self.asks if side == 'buy' else self.bids:
            take = min(remaining, level.qty)
            cost += take * level.price
            remaining -= take
            if remaining <= ZERO:
                return cost / qty
        return None


@dataclass(frozen=True)
class Fill:
    key: str
    market: Market
    side: Side
    qty: Decimal
    price: Decimal
    fee_usdc: Decimal
    base_fee: Decimal
    oid: str
    time_ms: int


@dataclass(frozen=True)
class Funding:
    key: str
    amount_usdc: Decimal
    time_ms: int


@dataclass(frozen=True)
class OpenOrder:
    oid: str
    cloid: str
    market: Market
    side: Side
    qty: Decimal
    price: Decimal
    created_ms: int


@dataclass(frozen=True)
class Account:
    spot_qty: Decimal
    perp_qty: Decimal
    equity: Decimal
    spot_cash: Decimal
    perp_withdrawable: Decimal
    foreign_activity: bool = False


@dataclass(frozen=True)
class Snapshot:
    now_ms: int
    spot: Book
    perp: Book
    account: Account
    orders: tuple[OpenOrder, ...] = ()
    fills: tuple[Fill, ...] = ()
    funding: tuple[Funding, ...] = ()
    healthy: bool = True


@dataclass(frozen=True)
class Action:
    kind: Literal['place', 'cancel']
    market: Market
    side: Side
    qty: Decimal
    price: Decimal
    cloid: str
    oid: str | None = None
    tif: Literal['Alo', 'Ioc'] = 'Alo'
    reduce_only: bool = False
    reason: str = ''

    def to_dict(self) -> dict:
        d = asdict(self)
        d['qty'], d['price'] = str(self.qty), str(self.price)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'Action':
        d = dict(data)
        d['qty'], d['price'] = D(d['qty']), D(d['price'])
        return cls(**d)


@dataclass(frozen=True)
class Result:
    status: Literal['accepted', 'filled', 'canceled', 'rejected', 'unknown']
    oid: str | None = None
    filled_qty: Decimal = ZERO
    error: str = ''


@dataclass(frozen=True)
class Precision:
    spot_sz_decimals: int
    perp_sz_decimals: int

    def qty_step(self, market: Market) -> Decimal:
        places = self.spot_sz_decimals if market == 'spot' else self.perp_sz_decimals
        return D(1).scaleb(-places)

    def floor_qty(self, qty: Decimal, market: Market) -> Decimal:
        return qty.quantize(self.qty_step(market), rounding=ROUND_DOWN)

    def price_step(self, price: Decimal, market: Market) -> Decimal:
        if price <= ZERO:
            raise ValueError('Price must be positive')
        places = (8 - self.spot_sz_decimals) if market == 'spot' else (6 - self.perp_sz_decimals)
        return D(1).scaleb(max(price.adjusted() - 4, -places, 0 if price >= 10000 else -100))

    def round_price(self, price: Decimal, market: Market, up: bool = False) -> Decimal:
        step = self.price_step(price, market)
        return (price / step).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * step
