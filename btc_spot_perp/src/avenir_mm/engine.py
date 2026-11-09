"""Single-owner BTC cycle ledger. Decisions are durable before network effects."""
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import uuid

from .config import Config
from .domain import Action, D, Fill, Precision, Result, Snapshot, ZERO


@dataclass
class EngineState:
    started_ms: int = 0
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    sequence: int = 0
    pending: Action | None = None
    pending_since_ms: int = 0
    pending_status: str = ''
    pending_reported_qty: Decimal = ZERO
    owned_actions: dict[str, Action] = field(default_factory=dict)
    oid_to_cloid: dict[str, str] = field(default_factory=dict)
    fill_qty_by_oid: dict[str, Decimal] = field(default_factory=dict)
    seen_fills: set[str] = field(default_factory=set)
    seen_funding: set[str] = field(default_factory=set)
    spot_qty: Decimal = ZERO
    perp_qty: Decimal = ZERO
    cycle_active: bool = False
    cycle_started_ms: int = 0
    cycle_cashflow: Decimal = ZERO
    entry_notional: Decimal = ZERO
    completed_cycles: int = 0
    realized_pnl: Decimal = ZERO
    last_maker_attempt_ms: int = 0
    dust_since_ms: int = 0
    hedge_rejections: int = 0
    halted_reason: str = ''
    overall_equity_peak: Decimal = ZERO
    equity_history: list[tuple[int, Decimal]] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = dict(vars(self))
        for key, value in list(result.items()):
            if isinstance(value, Decimal): result[key] = str(value)
            elif isinstance(value, set): result[key] = sorted(value)
        result['pending'] = self.pending.to_dict() if self.pending else None
        result['owned_actions'] = {k:v.to_dict() for k,v in self.owned_actions.items()}
        result['fill_qty_by_oid'] = {k:str(v) for k,v in self.fill_qty_by_oid.items()}
        result['equity_history'] = [[t,str(v)] for t,v in self.equity_history]
        return result

    @classmethod
    def from_dict(cls, data: dict) -> 'EngineState':
        d = dict(data)
        for name in ('pending_reported_qty','spot_qty','perp_qty','cycle_cashflow','entry_notional','realized_pnl','overall_equity_peak'):
            if name in d: d[name] = D(d[name])
        d['pending'] = Action.from_dict(d['pending']) if d.get('pending') else None
        d['owned_actions'] = {k:Action.from_dict(v) for k,v in d.get('owned_actions',{}).items()}
        d['fill_qty_by_oid'] = {k:D(v) for k,v in d.get('fill_qty_by_oid',{}).items()}
        d['seen_fills'] = set(d.get('seen_fills',[]))
        d['seen_funding'] = set(d.get('seen_funding',[]))
        d['equity_history'] = [(int(t),D(v)) for t,v in d.get('equity_history',[])]
        return cls(**d)


def profitable_exit_price(cashflow: Decimal, target_profit: Decimal, spot_qty: Decimal,
                          cover_qty: Decimal, cover_vwap: Decimal,
                          spot_fee: Decimal, perp_fee: Decimal) -> Decimal:
    """Minimum sale price for the whole remaining position, including paid fees."""
    if spot_qty <= ZERO:
        raise ValueError('Exit needs positive spot quantity')
    return (cover_qty * cover_vwap * (1 + perp_fee) + target_profit - cashflow) / (spot_qty * (1 - spot_fee))


class Engine:
    def __init__(self, config: Config, precision: Precision, state: EngineState | dict | None = None):
        self.config, self.precision = config, precision
        self.state = EngineState.from_dict(state) if isinstance(state,dict) else state or EngineState()

    def _halt(self, reason: str):
        if not self.state.halted_reason:
            self.state.halted_reason = reason

    def _action(self, snapshot: Snapshot, **kwargs) -> Action:
        st = self.state
        if st.pending is not None:
            raise RuntimeError('Unresolved command already exists')
        st.sequence += 1
        token = hashlib.sha256(f'{st.instance_id}:{st.sequence}'.encode()).hexdigest()[:28]
        action = Action(cloid='0xa7e1'+token, **kwargs)
        st.pending, st.pending_since_ms = action, snapshot.now_ms
        st.pending_status, st.pending_reported_qty = '', ZERO
        st.owned_actions[action.cloid] = action
        if action.kind == 'place' and action.market == 'spot':
            st.last_maker_attempt_ms = snapshot.now_ms
        return action

    def _clear_pending(self):
        self.state.pending = None
        self.state.pending_since_ms = 0
        self.state.pending_status = ''
        self.state.pending_reported_qty = ZERO

    def _pending_fills_complete(self) -> bool:
        st = self.state
        if not st.pending or st.pending_reported_qty <= ZERO:
            return False
        oid = next((o for o,c in st.oid_to_cloid.items() if c == st.pending.cloid),st.pending.oid)
        return bool(oid and st.fill_qty_by_oid.get(str(oid),ZERO) >= st.pending_reported_qty)

    def record_result(self, action: Action, result: Result):
        st = self.state
        if st.pending != action:
            raise ValueError('Result does not match the durable pending command')
        if result.oid and action.kind == 'place':
            st.oid_to_cloid[str(result.oid)] = action.cloid
        st.pending_status = result.status
        st.pending_reported_qty = max(st.pending_reported_qty,result.filled_qty)
        if result.status == 'unknown':
            return
        if result.status == 'filled' or result.filled_qty > ZERO:
            if self._pending_fills_complete(): self._clear_pending()
            return
        if result.status == 'accepted':
            # ACK alone does not prove openOrders has caught up. Retain the intent
            # until its live order or full actual execution is observable.
            return
        if result.status in ('rejected','canceled'):
            if action.kind == 'place' and action.market == 'perp':
                st.hedge_rejections += 1
                if st.hedge_rejections >= self.config.max_hedge_rejections:
                    self._halt('hedge_rejections_exhausted')
            self._clear_pending()

    def _ingest(self, s: Snapshot) -> bool:
        st = self.state
        for o in s.orders:
            if o.cloid in st.owned_actions:
                st.oid_to_cloid[str(o.oid)] = o.cloid
                if st.pending and st.pending.cloid==o.cloid and st.pending_status=='accepted':
                    self._clear_pending()
        deferred = False
        for f in sorted(s.fills,key=lambda x:(x.time_ms,x.key)):
            if f.key in st.seen_fills or f.time_ms < st.started_ms:
                continue
            oid = str(f.oid)
            if oid not in st.oid_to_cloid:
                if st.pending and st.pending.kind == 'place':
                    deferred = True
                    continue
                self._halt('unrecognized_fill')
                deferred = True
                continue
            original = st.owned_actions[st.oid_to_cloid[oid]]
            if original.market != f.market or original.side != f.side:
                self._halt('fill_order_identity_mismatch')
                deferred = True
                continue
            signed = f.qty if f.side == 'buy' else -f.qty
            if f.market == 'spot':
                st.spot_qty += signed - f.base_fee
                if f.side == 'buy':
                    if not st.cycle_active:
                        st.cycle_active,st.cycle_started_ms = True,f.time_ms
                    st.entry_notional += f.qty * f.price
            else:
                st.perp_qty += signed
                st.hedge_rejections = 0
            st.cycle_cashflow += -signed * f.price - f.fee_usdc
            st.fill_qty_by_oid[oid] = st.fill_qty_by_oid.get(oid,ZERO) + f.qty
            st.seen_fills.add(f.key)
        for f in s.funding:
            if f.key not in st.seen_funding and f.time_ms >= st.started_ms:
                st.cycle_cashflow += f.amount_usdc
                st.seen_funding.add(f.key)
        if st.pending and st.pending_status=='accepted' and st.pending.market=='spot':
            oid=next((o for o,c in st.oid_to_cloid.items() if c==st.pending.cloid),None)
            if oid and st.fill_qty_by_oid.get(oid,ZERO)>=st.pending.qty:
                self._clear_pending()
        if self._pending_fills_complete(): self._clear_pending()
        return deferred

    def _owned_orders(self, s: Snapshot):
        return [o for o in s.orders if o.cloid in self.state.owned_actions or str(o.oid) in self.state.oid_to_cloid]

    def _cancel(self, s: Snapshot, orders, reason: str) -> Action | None:
        if not orders: return None
        o = orders[0]
        return self._action(s,kind='cancel',market=o.market,side=o.side,qty=o.qty,price=o.price,
                            oid=str(o.oid),reason=reason)

    def _fresh(self, s: Snapshot) -> bool:
        return s.healthy and all(b.bids and b.asks and b.bid < b.ask and
            0 <= s.now_ms-b.time_ms <= self.config.max_book_age_ms and
            0 <= s.now_ms-b.received_ms <= self.config.max_book_age_ms for b in (s.spot,s.perp))

    def _equity_risk(self, s: Snapshot):
        st,c = self.state,self.config
        st.overall_equity_peak = max(st.overall_equity_peak,s.account.equity)
        st.equity_history = [(t,v) for t,v in st.equity_history if t >= s.now_ms-86400000]
        # Minute anchors prevent unbounded state growth while current equity is checked each tick.
        if not st.equity_history or s.now_ms//60000 != st.equity_history[-1][0]//60000:
            st.equity_history.append((s.now_ms,s.account.equity))
        elif s.account.equity > st.equity_history[-1][1]:
            st.equity_history[-1] = (s.now_ms,s.account.equity)
        if st.overall_equity_peak-s.account.equity >= c.overall_drawdown_usdc:
            self._halt('overall_drawdown_limit')
        elif max(v for _,v in st.equity_history)-s.account.equity >= c.daily_loss_usdc:
            self._halt('rolling_24h_loss_limit')

    def _hedge(self, s: Snapshot, debt: Decimal) -> Action | None:
        c,st,p = self.config,self.state,self.precision
        qty = p.floor_qty(abs(debt),'perp')
        if qty == ZERO: return None
        side = 'sell' if debt > ZERO else 'buy'
        vw = s.perp.vwap(side,qty)
        if vw is None:
            self._halt('insufficient_visible_hedge_depth')
            return None
        if qty * vw < c.min_notional_usdc:
            return None
        if st.hedge_rejections >= c.max_hedge_rejections:
            return None
        best = s.perp.bid if side == 'sell' else s.perp.ask
        slip = ((best-vw)/best if side == 'sell' else (vw-best)/best)*10000
        if slip > c.slippage_bp:
            self._halt('hedge_slippage_limit')
            return None
        limit = best * (1-c.slippage_bp/10000 if side=='sell' else 1+c.slippage_bp/10000)
        # Round inside the slippage cap, never beyond it.
        price = p.round_price(limit,'perp',up=side=='sell')
        reduce_only = side == 'buy' and st.perp_qty < ZERO and qty <= -st.perp_qty
        return self._action(s,kind='place',market='perp',side=side,qty=qty,price=price,
                            tif='Ioc',reduce_only=reduce_only,reason='match_actual_spot_fill')

    def _entry(self, s: Snapshot, orders) -> Action | None:
        c,st,p = self.config,self.state,self.precision
        qty = p.floor_qty(p.floor_qty(c.order_notional_usdc/s.perp.bid,'perp'),'spot')
        vw = s.perp.vwap('sell',qty)
        if vw is None: return self._cancel(s,orders,'entry_depth_unavailable')
        target = p.round_price(vw*(1-c.entry_offset_bp/10000),'spot')
        target = min(target,p.round_price(s.spot.ask-p.price_step(s.spot.ask,'spot'),'spot'))
        if qty*target < c.min_notional_usdc:
            return self._cancel(s,orders,'below_minimum_order')
        if qty*target*(1+c.spot_maker_fee) > s.account.spot_cash or qty*vw > c.max_inventory_usdc:
            return self._cancel(s,orders,'insufficient_spot_budget')
        # Reserve enough collateral for a 1x hedge; adapter verifies the actual configured margin.
        if qty*vw*(1+c.perp_taker_fee+c.slippage_bp/10000) > s.account.perp_withdrawable:
            return self._cancel(s,orders,'insufficient_hedge_reserve')
        if orders:
            o=orders[0]
            edge=(vw-o.price)/vw*10000
            if o.side != 'buy' or edge < c.safety_cancel_edge_bp:
                return self._cancel(s,orders,'entry_edge_deteriorated')
            if s.now_ms-o.created_ms < c.quote_min_life_ms: return None
            if o.price != target and abs(target/o.price-1)*10000 >= c.reprice_bp:
                return self._cancel(s,orders,'entry_reprice')
            return None
        if st.last_maker_attempt_ms and s.now_ms-st.last_maker_attempt_ms < c.quote_min_life_ms:
            return None
        return self._action(s,kind='place',market='spot',side='buy',qty=qty,price=target,reason='entry_100bp')

    def _exit(self, s: Snapshot, orders) -> Action | None:
        c,st,p = self.config,self.state,self.precision
        qty = p.floor_qty(st.spot_qty,'spot')
        cover_qty = max(ZERO,-st.perp_qty)
        vw = s.perp.vwap('buy',cover_qty) if cover_qty else s.perp.ask
        if vw is None:
            self._halt('exit_depth_unavailable')
            return self._cancel(s,orders,'exit_depth_unavailable')
        target_profit=st.entry_notional*(c.minimum_profit_bp+c.execution_reserve_bp)/10000
        raw=profitable_exit_price(st.cycle_cashflow,target_profit,qty,cover_qty,vw,c.spot_maker_fee,c.perp_taker_fee)
        price=p.round_price(max(raw,s.spot.bid+p.price_step(s.spot.bid,'spot')),'spot',up=True)
        if qty*price < c.min_notional_usdc:
            self._halt('spot_exit_below_minimum')
            return self._cancel(s,orders,'spot_exit_below_minimum')
        if orders:
            o=orders[0]
            if o.side!='sell' or o.qty!=qty or o.price<raw:
                return self._cancel(s,orders,'exit_profit_or_size_changed')
            if s.now_ms-o.created_ms>=c.quote_min_life_ms and abs(price/o.price-1)*10000>=c.reprice_bp:
                return self._cancel(s,orders,'exit_reprice')
            return None
        return self._action(s,kind='place',market='spot',side='sell',qty=qty,price=price,reason='fee_aware_exit')

    def step(self, s: Snapshot) -> Action | None:
        st,c,p=self.state,self.config,self.precision
        if not st.started_ms: st.started_ms=s.now_ms
        deferred=self._ingest(s)
        orders=self._owned_orders(s)
        if st.pending: return None
        self._equity_risk(s)
        if not self._fresh(s):
            self._halt('stale_or_unhealthy_data')
            return self._cancel(s,orders,'stale_or_unhealthy_data')
        mismatch=(abs(s.account.spot_qty-st.spot_qty)>=p.qty_step('spot') or
                  abs(s.account.perp_qty-st.perp_qty)>=p.qty_step('perp'))
        if s.account.foreign_activity or mismatch or deferred:
            self._halt('account_reconciliation_required')
            return self._cancel(s,orders,'account_reconciliation_required')
        if st.spot_qty < ZERO or st.perp_qty > ZERO:
            self._halt('unexpected_position_direction')
            return self._cancel(s,orders,'unexpected_position_direction')
        debt=st.spot_qty+st.perp_qty
        if st.spot_qty*s.spot.bid>c.max_inventory_usdc:
            self._halt('inventory_limit')
        debt_price=s.perp.bid if debt>=ZERO else s.perp.ask
        if abs(debt)*debt_price>c.max_unhedged_usdc:
            self._halt('unhedged_exposure_limit')
        if abs(debt)>=p.qty_step('perp'):
            if not st.dust_since_ms: st.dust_since_ms=s.now_ms
            action=self._hedge(s,debt)
            if action: return action
            if s.now_ms-st.dust_since_ms>=c.dust_wait_ms:
                self._halt('unhedgeable_residual_timeout')
            if st.halted_reason: return self._cancel(s,orders,st.halted_reason)
            # Retain the existing entry/exit remainder briefly so small fills may aggregate.
            return None
        st.dust_since_ms=0
        if st.cycle_active and st.spot_qty==ZERO and st.perp_qty==ZERO and not orders:
            st.realized_pnl+=st.cycle_cashflow
            st.completed_cycles+=1
            st.cycle_active=False
            st.cycle_started_ms=0
            st.cycle_cashflow=ZERO
            st.entry_notional=ZERO
        if st.cycle_active and s.now_ms-st.cycle_started_ms>=c.max_holding_ms:
            self._halt('maximum_holding_time')
        if st.halted_reason: return self._cancel(s,orders,st.halted_reason)
        if st.cycle_active:
            entries=[o for o in orders if o.side=='buy']
            if entries: return self._cancel(s,entries,'cancel_entry_remainder')
            if st.spot_qty==ZERO and st.perp_qty==ZERO:
                if orders: return self._cancel(s,orders,'cycle_complete')
            elif st.spot_qty>ZERO:
                return self._exit(s,orders)
        return self._entry(s,orders)
