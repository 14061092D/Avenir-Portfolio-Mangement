"""Validated Hyperliquid SDK boundary; no implicit transfers or account changes.

Live construction requires an explicit operator flag. An HTTP acknowledgement is
never a ledger fill. Submission ambiguity is returned to the durable controller.
"""
from __future__ import annotations

import math
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Callable, Iterable

from .domain import Account, Action, Book, D, Fill, Funding, Level, OpenOrder, Precision, Result, Snapshot, ZERO

UBTC_TOKEN_ID = '0x8f254b963e8468305d409b33aa137c67'
CLOID_PREFIX = '0xa7e1'
MAINNET_URL = 'https://api.hyperliquid.xyz'


class VenueError(RuntimeError):
    """Malformed, unsupported or unsafe external state; stop opening orders."""


class RateBudgetExceeded(VenueError):
    pass


class SnapshotChanged(VenueError):
    """Position changed during a read; obtain a new read without taking action."""


class SubmissionNotSent(VenueError):
    """Local guard failed before invoking the SDK mutation; safe to mark rejected."""


def number(value, field: str) -> Decimal:
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise VenueError(f'Invalid numeric field: {field}') from exc
    if not result.is_finite():
        raise VenueError(f'Non-finite numeric field: {field}')
    return result


@dataclass(frozen=True)
class PairMetadata:
    spot_coin: str
    perp_coin: str
    spot_token_index: int
    quote_token_index: int
    precision: Precision
    spot_meta: dict
    perp_meta: dict


def discover_pair(info) -> PairMetadata:
    """Match by stable token ID and quote identity, not a display ticker."""
    sm, pm = info.spot_meta(), info.meta()
    try:
        tokens = {int(t['index']): t for t in sm['tokens']}
        bases = [t for t in tokens.values() if t.get('tokenId', '').lower() == UBTC_TOKEN_ID]
        quotes = [t for t in tokens.values() if int(t['index']) == 0 and t['name'] == 'USDC']
        if len(bases) != 1 or len(quotes) != 1 or bases[0]['name'] != 'UBTC':
            raise VenueError('UBTC/USDC identity cannot be verified')
        base, quote = bases[0], quotes[0]
        matches = [x for x in sm['universe'] if x['tokens'] == [base['index'], quote['index']]]
        perps = [x for x in pm['universe'] if x['name'] == 'BTC' and not x.get('isDelisted')]
        if len(matches) != 1 or len(perps) != 1:
            raise VenueError('Expected one active UBTC/USDC spot and BTC perpetual')
        sp, pp = int(base['szDecimals']), int(perps[0]['szDecimals'])
        if not 0 <= sp <= 8 or not 0 <= pp <= 6:
            raise VenueError('Unsupported quantity precision')
        expected = '@' + str(matches[0]['index'])
        if matches[0]['name'] != expected:
            raise VenueError('Spot identifier differs from market index')
        return PairMetadata(expected, 'BTC', int(base['index']), 0,
                            Precision(sp, pp), sm, pm)
    except (KeyError, TypeError, ValueError) as exc:
        raise VenueError('Malformed metadata') from exc


def parse_book(raw: dict, received_ms: int) -> Book:
    try:
        sides = []
        for raw_side in raw['levels']:
            side = tuple(Level(number(x['px'], 'book price'), number(x['sz'], 'book quantity')) for x in raw_side)
            if not side or any(x.price <= 0 or x.qty <= 0 for x in side):
                raise VenueError('Empty or non-positive book')
            sides.append(side)
        if len(sides) != 2:
            raise VenueError('Book must contain two sides')
        bids, asks = sides
        if any(a.price <= b.price for a, b in zip(bids, bids[1:])) or any(a.price >= b.price for a, b in zip(asks, asks[1:])):
            raise VenueError('Unsorted or duplicate book levels')
        if bids[0].price >= asks[0].price:
            raise VenueError('Crossed book')
        timestamp = int(raw['time'])
        if timestamp <= 0 or timestamp > received_ms + 1000:
            raise VenueError('Invalid book timestamp')
        return Book(str(raw['coin']), timestamp, received_ms, bids, asks)
    except (KeyError, TypeError, ValueError) as exc:
        raise VenueError('Malformed L2 book') from exc


class WeightBudget:
    """One-process conservative REST budget; other processes share venue IP quota."""
    def __init__(self, limit: int = 300, clock_ms: Callable[[], int] | None = None, *, emergency_reserve=0):
        self.limit = limit
        self.emergency_reserve = emergency_reserve
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.entries: deque[tuple[int, int]] = deque()

    def consume(self, weight: int, *, critical=False):
        now = self.clock_ms()
        while self.entries and self.entries[0][0] <= now - 60_000:
            self.entries.popleft()
        ceiling = self.limit if critical else self.limit-self.emergency_reserve
        if sum(x[1] for x in self.entries) + weight > ceiling:
            raise RateBudgetExceeded('Local REST weight reserve exhausted; do not retry blindly')
        self.entries.append((now, weight))


class LiveVenue:
    """Serial broker. Use from_env only from an explicitly operator-selected mode.

    Account endpoints always use the trading account, never the agent address.
    Tests may inject SDK fakes into this constructor without reading secrets.
    """
    def __init__(self, config, pair: PairMetadata, info, exchange, address: str,
                 books: Callable[[], tuple[Book, Book]], *, clock_ms=None,
                 rest_weight_per_minute=900, account_action_reserve=1000,
                 reconcile_ms=15_000, action_ttl_ms=5000):
        if not re.fullmatch(r'0x[0-9a-fA-F]{40}', address):
            raise VenueError('Invalid trading account address')
        self.config, self.pair, self.info, self.exchange = config, pair, info, exchange
        self.address, self.books = address, books
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.budget = WeightBudget(rest_weight_per_minute, self.clock_ms, emergency_reserve=200)
        self.account_action_reserve = account_action_reserve
        self.reconcile_ms, self.action_ttl_ms = reconcile_ms, action_ttl_ms
        self._lock = threading.Lock()
        self._known: dict[str, Action] = {}
        self._oid_cloid: dict[str, str] = {}
        self._sent: set[str] = set()
        self._cache: Snapshot | None = None
        self._last_account_ms = 0
        self._last_rate_ms = 0
        self._last_funding_ms = 0
        self._last_deadman_ms = 0
        self._funding: tuple[Funding, ...] = ()
        self._remaining: int | None = None
        self._preflight_done = False

    @classmethod
    def from_env(cls, config, pair: PairMetadata, books, *, operator_confirmed: bool = False,
                 base_url: str = MAINNET_URL):
        if not operator_confirmed or config.mode != 'live':
            raise VenueError('Live mode requires explicit operator selection')
        if base_url != MAINNET_URL:
            raise VenueError('This reviewed adapter supports the mainnet BTC/UBTC mapping only')
        # Read the key only after the explicit guard above. Never log it.
        address = os.environ.get('HL_ACCOUNT_ADDRESS', '')
        private_key = os.environ.get('HL_AGENT_PRIVATE_KEY', '')
        if not address or not private_key:
            raise VenueError('Set HL_ACCOUNT_ADDRESS and HL_AGENT_PRIVATE_KEY locally')
        from eth_account import Account as EthAccount
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        try:
            wallet = EthAccount.from_key(private_key)
        except Exception as exc:
            raise VenueError('Invalid agent signing key') from exc
        info = Info(base_url, skip_ws=True, meta=pair.perp_meta, spot_meta=pair.spot_meta, timeout=5)
        role = info.post('/info', {'type': 'userRole', 'user': wallet.address})
        if not isinstance(role, dict) or role.get('role') != 'agent' or str(role.get('data', {}).get('user', '')).lower() != address.lower():
            raise VenueError('Signing key must be an API agent approved for HL_ACCOUNT_ADDRESS')
        exchange = Exchange(wallet, base_url, meta=pair.perp_meta, spot_meta=pair.spot_meta,
                            account_address=address, timeout=5)
        return cls(config, pair, info, exchange, address, books)

    def register_actions(self, actions: Iterable[Action], oid_to_cloid: dict[str, str] | None = None):
        for action in actions:
            self._known[action.cloid] = action
            if action.oid is not None:
                if action.kind == 'place':
                    self._oid_cloid[str(action.oid)] = action.cloid
        if oid_to_cloid:
            for oid, cloid in oid_to_cloid.items():
                if cloid not in self._known or self._known[cloid].kind != 'place':
                    raise VenueError('Persisted order ownership does not match an issued placement')
                self._oid_cloid[str(oid)] = cloid

    def _read(self, weight, method, *args, critical=False):
        self.budget.consume(weight, critical=critical)
        try:
            return method(*args)
        except Exception as exc:
            raise VenueError(f'Account read failed: {getattr(method, "__name__", "query")}') from exc

    def _refresh_rate(self, force=False):
        if self._remaining is not None and not force and self.clock_ms() - self._last_rate_ms < 60_000:
            return
        raw = self._read(20, self.info.user_rate_limit, self.address)
        try:
            cap, used = int(raw['nRequestsCap']), int(raw['nRequestsUsed'])
            if cap < 0 or used < 0:
                raise ValueError('negative quota')
            self._remaining = max(0, cap-used)
            self._last_rate_ms = self.clock_ms()
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError('Cannot verify address action quota') from exc

    def preflight(self, since_ms: int) -> Snapshot:
        """Read-only checks; no deadman/order action is submitted by preflight."""
        mode = self._read(20, self.info.post, '/info', {'type': 'userAbstraction', 'user': self.address})
        if mode != 'disabled':
            raise VenueError('Only explicitly disabled/classic account abstraction is supported; unknown/default/unified/portfolio modes are blocked')
        role = self._read(60, self.info.post, '/info', {'type': 'userRole', 'user': self.address})
        if not isinstance(role, dict) or role.get('role') != 'user':
            raise VenueError('Dedicated ordinary trading account required; vault/subaccount semantics are not enabled')
        self._refresh_rate(True)
        # Recover order ownership when openOrders omits client IDs.
        for action in list(self._known.values()):
            if action.kind == 'place' and action.cloid not in self._oid_cloid.values():
                self.resolve(action)
        snap = self.snapshot(since_ms, force=True)
        if snap.account.foreign_activity:
            raise VenueError('Foreign orders, assets, positions or funding in dedicated account')
        if not self._known:
            if snap.orders or snap.account.spot_qty != ZERO or snap.account.perp_qty != ZERO:
                raise VenueError('Fresh strategy requires an empty BTC position and no open orders')
            if snap.account.equity > self.config.capital_usdc * D('1.01'):
                raise VenueError('Account exceeds configured pilot capital; use a dedicated funded account')
            if snap.account.spot_cash < self.config.order_notional_usdc or snap.account.perp_withdrawable < D('50'):
                raise VenueError('Fund spot order cash and at least 50 USDC perp reserve manually')
        self._preflight_done = True
        return snap

    def _market(self, coin):
        if coin == self.pair.spot_coin:
            return 'spot'
        if coin == self.pair.perp_coin:
            return 'perp'
        raise VenueError('Unexpected account asset or fill')

    def _parse_fills(self, raws) -> tuple[Fill, ...]:
        if not isinstance(raws, list) or len(raws) >= 2000:
            raise VenueError('Fill response malformed or potentially truncated; reconciliation required')
        fills = {}
        try:
            for r in raws:
                market = self._market(r['coin'])
                fee = number(r['fee'], 'fill fee')
                token = r.get('feeToken')
                if token == 'USDC':
                    fee_usdc, base_fee = fee, ZERO
                elif market == 'spot' and token == 'UBTC':
                    fee_usdc, base_fee = ZERO, fee
                else:
                    raise VenueError('Unknown fill fee token')
                if r['side'] not in ('A', 'B'):
                    raise VenueError('Unknown fill side')
                key = f"{int(r['time'])}:{r['coin']}:{r['tid']}"
                f = Fill(key, market, 'buy' if r['side']=='B' else 'sell', number(r['sz'], 'fill size'),
                         number(r['px'], 'fill price'), fee_usdc, base_fee, str(r['oid']), int(r['time']))
                if f.qty <= 0 or f.price <= 0:
                    raise VenueError('Non-positive fill')
                if key in fills and fills[key] != f:
                    raise VenueError('Conflicting fill identity')
                fills[key] = f
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError('Malformed account fill') from exc
        return tuple(sorted(fills.values(), key=lambda f: (f.time_ms, f.key)))

    def snapshot(self, since_ms: int, *, force=False) -> Snapshot:
        spot, perp = self.books()
        if spot.coin != self.pair.spot_coin or perp.coin != 'BTC':
            raise VenueError('Book identity mismatch')
        now = self.clock_ms()
        if self._cache and not force and now-self._last_account_ms < self.reconcile_ms:
            return replace(self._cache, now_ms=now, spot=spot, perp=perp)
        raw_spot = self._read(2, self.info.spot_user_state, self.address, critical=force)
        raw_perp = self._read(2, self.info.user_state, self.address, critical=force)
        raw_orders = self._read(20, self.info.open_orders, self.address, critical=force)
        raw_fills = self._read(20, self.info.user_fills_by_time, self.address, since_ms, None, False, critical=force)
        if isinstance(raw_fills, list):
            self.budget.consume(math.ceil(len(raw_fills)/20), critical=force)
        fills = self._parse_fills(raw_fills)
        # REST account reads are not atomic across markets. A position change
        # during the read cannot be treated as an authoritative reconciliation.
        spot_after = self._read(2, self.info.spot_user_state, self.address, critical=force)
        perp_after = self._read(2, self.info.user_state, self.address, critical=force)
        try:
            def spot_signature(raw):
                return sorted((int(r['token']), number(r['total'],'spot total'), number(r['hold'],'spot hold')) for r in raw['balances'])
            def perp_signature(raw):
                return sorted((r['position']['coin'], number(r['position']['szi'],'perp size')) for r in raw['assetPositions'])
            if spot_signature(raw_spot) != spot_signature(spot_after) or perp_signature(raw_perp) != perp_signature(perp_after):
                self._last_account_ms = 0
                raise SnapshotChanged('Account changed during reconciliation; retry a fresh read without submitting an order')
            raw_spot, raw_perp = spot_after, perp_after
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError('Malformed account consistency check') from exc
        foreign, spot_qty, spot_total_cash, spot_cash, perp_qty = False, ZERO, ZERO, ZERO, ZERO
        try:
            for r in raw_spot['balances']:
                total, hold = number(r['total'], 'spot total'), number(r['hold'], 'spot hold')
                if total < 0 or hold < 0 or hold > total:
                    raise VenueError('Invalid spot balance/hold')
                if int(r['token']) == self.pair.spot_token_index and r['coin']=='UBTC':
                    spot_qty += total
                elif int(r['token']) == 0 and r['coin']=='USDC':
                    spot_total_cash += total
                    spot_cash += total-hold
                elif total != ZERO:
                    foreign = True
            for r in raw_perp['assetPositions']:
                q = r['position']; qty = number(q['szi'], 'perp quantity')
                if q['coin'] == 'BTC':
                    perp_qty += qty
                    if qty > ZERO:
                        foreign = True
                elif qty != ZERO:
                    foreign = True
            perp_equity = number(raw_perp['marginSummary']['accountValue'], 'perp equity')
            withdrawable = number(raw_perp['withdrawable'], 'perp withdrawable')
            orders = []
            if not isinstance(raw_orders, list):
                raise VenueError('Malformed open orders')
            for r in raw_orders:
                oid = str(r['oid']); cloid = r.get('cloid') or self._oid_cloid.get(oid, '')
                if r['coin'] not in (self.pair.spot_coin, 'BTC') or cloid not in self._known:
                    foreign = True
                    continue
                market = self._market(r['coin'])
                size, price = number(r['sz'], 'order size'), number(r['limitPx'], 'order price')
                if r['side'] not in ('A','B') or size <= ZERO or price <= ZERO:
                    raise VenueError('Invalid open-order side, size or price')
                orders.append(OpenOrder(oid, cloid, market, 'buy' if r['side']=='B' else 'sell',
                                        size, price, int(r['timestamp'])))
                self._oid_cloid[oid] = cloid
            if now-self._last_funding_ms >= 60_000 or not self._last_funding_ms:
                funding_raw = self._read(20, self.info.user_funding_history, self.address, since_ms, now)
                if not isinstance(funding_raw, list) or len(funding_raw) >= 500:
                    raise VenueError('Funding response malformed or potentially truncated')
                self.budget.consume(math.ceil(len(funding_raw)/20))
                funding = []
                for r in funding_raw:
                    delta = r['delta']
                    if delta['coin'] != 'BTC':
                        foreign = True
                        continue
                    funding.append(Funding(f"{r['time']}:{r['hash']}:BTC", number(delta['usdc'], 'funding'), int(r['time'])))
                self._funding, self._last_funding_ms = tuple(funding), now
        except (KeyError, TypeError, ValueError) as exc:
            raise VenueError('Malformed account snapshot') from exc
        self._refresh_rate()
        spot, perp = self.books()
        now = self.clock_ms()
        account = Account(spot_qty, perp_qty, spot_total_cash+spot_qty*spot.bid+perp_equity,
                          spot_cash, withdrawable, foreign)
        self._cache = Snapshot(now, spot, perp, account, tuple(orders), fills, self._funding)
        self._last_account_ms = now
        return self._cache

    def _action_budget(self, *, opening: bool):
        self._refresh_rate()
        if self._remaining is None or self._remaining <= 0:
            raise RateBudgetExceeded('No verified address action capacity')
        if opening and self._remaining <= self.account_action_reserve:
            raise RateBudgetExceeded('Address reserve retained for hedge/cancel actions')
        self.budget.consume(1, critical=not opening)
        self._remaining -= 1

    def refresh_deadman(self, *, force=False) -> Result:
        if not self._preflight_done:
            raise VenueError('Run preflight before arming deadman')
        now = self.clock_ms()
        if not force and self._last_deadman_ms and now-self._last_deadman_ms < 30_000:
            return Result('accepted')
        with self._lock:
            self._action_budget(opening=False)
            self.exchange.set_expires_after(now+self.action_ttl_ms)
            try:
                raw = self.exchange.schedule_cancel(now+90_000)
            except Exception:
                return Result('unknown', error='Deadman schedule outcome unknown; freeze new orders')
            if not isinstance(raw, dict) or raw.get('status') != 'ok':
                return Result('rejected', error='Deadman schedule rejected; do not start/resume trading')
            response = raw.get('response')
            if not isinstance(response, dict) or response.get('type') != 'default':
                return Result('unknown', error='Unexpected deadman response')
            if isinstance(response.get('data'),dict) and 'error' in response['data']:
                return Result('rejected', error='Deadman schedule failed; do not trade')
            self._last_deadman_ms = now
            return Result('accepted')

    def _validate_action(self, action: Action):
        if not re.fullmatch(r'0xa7e1[0-9a-f]{28}', action.cloid):
            raise VenueError('Invalid strategy client order identifier')
        if action.kind == 'cancel':
            if action.oid is None or str(action.oid) not in self._oid_cloid:
                raise VenueError('Cannot cancel an order without verified ownership')
            return
        if action.qty <= ZERO or action.price <= ZERO:
            raise VenueError('Non-positive order')
        if self.pair.precision.floor_qty(action.qty, action.market) != action.qty or self.pair.precision.round_price(action.price, action.market) != action.price:
            raise VenueError('Order violates venue precision')
        if action.qty*action.price < self.config.min_notional_usdc:
            # No assumption that reduce-only is exempt. A sub-minimum residual is explicit.
            raise VenueError('Order below minimum notional; retain and report residual')
        spot, perp = self.books(); now = self.clock_ms()
        if any(now-b.time_ms > self.config.max_book_age_ms or now-b.received_ms > self.config.max_book_age_ms for b in (spot,perp)):
            raise VenueError('Stale market data')
        if action.market == 'spot':
            if action.tif != 'Alo' or action.reduce_only:
                raise VenueError('Spot orders must be post-only, without reduce-only flag')
            if (action.side=='buy' and action.price >= spot.ask) or (action.side=='sell' and action.price <= spot.bid):
                raise VenueError('Post-only order would cross')
        else:
            if action.tif != 'Ioc' or (action.side=='buy' and not action.reduce_only) or (action.side=='sell' and action.reduce_only):
                raise VenueError('BTC hedge must be bounded IOC; buys close existing shorts reduce-only')
            best = perp.ask if action.side=='buy' else perp.bid
            allowed = best*(D(1)+(self.config.slippage_bp/D(10000))*(D(1) if action.side=='buy' else D(-1)))
            if (action.side=='buy' and action.price > allowed) or (action.side=='sell' and action.price < allowed):
                raise VenueError('IOC price exceeds configured slippage boundary')

    @staticmethod
    def _parse_submit(raw, action: Action) -> Result:
        if not isinstance(raw, dict):
            return Result('unknown', error='Malformed submission response')
        if raw.get('status') == 'err':
            return Result('rejected', error='Exchange rejected action')
        try:
            if raw.get('status') != 'ok':
                return Result('unknown', error='Unrecognized submission status')
            statuses = raw['response']['data']['statuses']
            if len(statuses) != 1:
                return Result('unknown', error='Unexpected action status count')
            status = statuses[0]
            if action.kind=='cancel' and status=='success':
                return Result('canceled', action.oid)
            if isinstance(status, dict) and 'error' in status:
                # An unsuccessful cancel can mean already filled; resolve it.
                if action.kind=='cancel':
                    return Result('unknown', action.oid, error='Cancel not confirmed; resolve order')
                return Result('rejected', error=str(status['error']))
            if isinstance(status, dict) and 'resting' in status:
                return Result('accepted', str(status['resting']['oid']))
            if isinstance(status, dict) and 'filled' in status:
                f = status['filled']; qty=number(f['totalSz'], 'ACK filled size')
                if qty < ZERO or qty > action.qty:
                    return Result('unknown', error='Invalid ACK filled quantity')
                return Result('filled', str(f['oid']), qty)
        except (KeyError, TypeError, ValueError, VenueError):
            pass
        return Result('unknown', error='Malformed or ambiguous submission response')

    def submit(self, action: Action) -> Result:
        key=f'{action.kind}:{action.cloid}'
        if key in self._sent:
            return Result('unknown', error='Intent already sent; resolve instead of resubmitting')
        from hyperliquid.utils.types import Cloid
        with self._lock:
            # All these operations are local or read-only. Separate them from
            # the first call that can reach the exchange mutation endpoint.
            try:
                if not self._preflight_done:
                    raise VenueError('Run account preflight before submission')
                self._validate_action(action)
                if action.kind=='place' and self.clock_ms()-self._last_deadman_ms >= 90_000:
                    raise VenueError('Deadman has not been confirmed')
                coin=self.pair.spot_coin if action.market=='spot' else 'BTC'
                target=self._oid_cloid[str(action.oid)] if action.kind=='cancel' else action.cloid
                sdk_cloid=Cloid.from_str(target)
                self._action_budget(opening=action.kind=='place' and action.market=='spot' and action.side=='buy')
                self.exchange.set_expires_after(self.clock_ms()+self.action_ttl_ms)
            except Exception as exc:
                raise SubmissionNotSent(f'not_sent: {exc}') from exc
            self._known[action.cloid]=action
            self._sent.add(key)
            try:
                if action.kind=='cancel':
                    raw=self.exchange.cancel_by_cloid(coin, sdk_cloid)
                else:
                    raw=self.exchange.order(coin, action.side=='buy', float(action.qty), float(action.price),
                                            {'limit': {'tif': action.tif}}, reduce_only=action.reduce_only,
                                            cloid=sdk_cloid)
                result=self._parse_submit(raw, action)
            except Exception:
                result=Result('unknown', error='Submission outcome unknown; resolve client order ID before any retry')
            if result.oid is not None and action.kind=='place':
                self._oid_cloid[str(result.oid)]=action.cloid
            self._last_account_ms=0
            return result

    def resolve(self, action: Action) -> Result:
        from hyperliquid.utils.types import Cloid
        target=action.cloid
        if action.kind=='cancel':
            target=self._oid_cloid.get(str(action.oid),'')
            if not target:
                return Result('unknown',error='Cancel target ownership is unresolved')
        raw=self._read(2, self.info.query_order_by_cloid, self.address, Cloid.from_str(target), critical=True)
        if not isinstance(raw, dict) or raw.get('status')=='unknownOid':
            return Result('unknown', error='Order ID not yet known; absence is not proof of rejection')
        try:
            wrapper=raw['order']; status=wrapper['status']; order=wrapper['order']
            oid=str(order['oid'])
            if order.get('coin') not in (self.pair.spot_coin if action.market=='spot' else 'BTC',):
                return Result('unknown', error='Resolved order asset mismatch')
            if order.get('cloid') and order['cloid'] != target:
                return Result('unknown', error='Resolved client identifier mismatch')
            if order.get('side') and order['side'] != ('B' if action.side=='buy' else 'A'):
                return Result('unknown', error='Resolved order side mismatch')
            self._oid_cloid[oid]=target
            if status=='open':
                if action.kind=='cancel':
                    return Result('unknown',oid,error='Cancel still pending; target order is open')
                return Result('accepted', oid)
            if status=='filled':
                # origSz is a status observation, not a Fill to book.
                original_qty=number(order['origSz'], 'original size')
                original=self._known.get(target)
                if original_qty<=ZERO or (original is not None and original_qty!=original.qty):
                    return Result('unknown',error='Resolved original quantity mismatch')
                return Result('filled', oid, original_qty)
            if status in ('canceled','scheduledCancel','marginCanceled','selfTradeCanceled','liquidatedCanceled'):
                return Result('canceled', oid)
            if status.endswith('Rejected') or status=='rejected':
                return Result('rejected', oid, error=status)
        except (KeyError, TypeError, ValueError, VenueError):
            pass
        return Result('unknown', error='Unrecognized order resolution')
