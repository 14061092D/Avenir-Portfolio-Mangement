"""Conservative public-print paper execution; never sends exchange actions."""
from dataclasses import asdict
import time
from .domain import Account, Action, Book, D, Fill, OpenOrder, Result, Snapshot, ZERO


class PaperVenue:
    def __init__(self, config, pair, books, state=None, clock_ms=None, participation=D('0.1')):
        self.config, self.pair, self.books = config, pair, books
        self.clock_ms = clock_ms or (lambda: int(time.time()*1000))
        self.participation = participation
        self.cash = config.capital_usdc / 2
        self.perp_cash = config.capital_usdc / 2
        self.spot_qty = self.perp_qty = self.perp_flow = ZERO
        self.orders, self.results, self.fills, self.seen = {}, {}, [], set()
        self.counter = 0
        if state:
            for k in ('cash','perp_cash','spot_qty','perp_qty','perp_flow'):
                setattr(self,k,D(state[k]))
            self.counter = state['counter']
            self.seen = set(state['seen'])
            for raw in state['orders']:
                x=dict(raw); x['qty']=D(x['qty']);x['price']=D(x['price'])
                o=OpenOrder(**x); self.orders[o.oid]=o
            for raw in state['fills']:
                x=dict(raw)
                for k in ('qty','price','fee_usdc','base_fee'): x[k]=D(x[k])
                self.fills.append(Fill(**x))
            for key,raw in state['results'].items():
                x=dict(raw);x['filled_qty']=D(x['filled_qty']);self.results[key]=Result(**x)

    def to_dict(self):
        return {'cash':str(self.cash),'perp_cash':str(self.perp_cash),'spot_qty':str(self.spot_qty),
                'perp_qty':str(self.perp_qty),'perp_flow':str(self.perp_flow),'counter':self.counter,
                'orders':[asdict(o) for o in self.orders.values()], 'fills':[asdict(f) for f in self.fills],
                'results':{k:asdict(v) for k,v in self.results.items()},'seen':sorted(self.seen)}

    def register_actions(self, actions, oid_to_cloid=None):
        pass  # Paper order IDs and results already persist with the account.

    def _fill(self, oid, market, side, qty, price, key, time_ms):
        fee=qty*price*(self.config.spot_maker_fee if market=='spot' else self.config.perp_taker_fee)
        signed=qty if side=='buy' else -qty
        if market=='spot':
            self.spot_qty += signed
            self.cash -= signed*price+fee
        else:
            self.perp_qty += signed
            self.perp_flow -= signed*price+fee
        self.fills.append(Fill(key,market,side,qty,price,fee,ZERO,oid,time_ms))

    def observe_trade(self, raw):
        key=f"{raw['time']}:{raw['coin']}:{raw['tid']}"
        if key in self.seen or raw['coin']!=self.pair.spot_coin:
            return
        self.seen.add(key)
        for oid,o in list(self.orders.items()):
            # Initial snapshots and trades before modeled order arrival cannot fill.
            if int(raw['time']) <= o.created_ms+200:
                continue
            price=D(raw['px'])
            crosses=(o.side=='buy' and raw['side']=='A' and price<o.price) or (o.side=='sell' and raw['side']=='B' and price>o.price)
            if not crosses:
                continue
            qty=self.pair.precision.floor_qty(min(o.qty,D(raw['sz'])*self.participation),'spot')
            if qty<=ZERO:
                continue
            self._fill(oid,'spot',o.side,qty,o.price,'paper:'+key+':'+oid,int(raw['time']))
            remaining=o.qty-qty
            if remaining>ZERO:
                self.orders[oid]=OpenOrder(o.oid,o.cloid,o.market,o.side,remaining,o.price,o.created_ms)
            else:
                del self.orders[oid]
                total=sum((f.qty for f in self.fills if f.oid==oid),ZERO)
                self.results[o.cloid]=Result('filled',oid,total)

    def snapshot(self, since_ms, force=False):
        spot,perp=self.books()
        mark=(perp.bid+perp.ask)/2
        peq=self.perp_cash+self.perp_flow+self.perp_qty*mark
        equity=self.cash+self.spot_qty*(spot.bid+spot.ask)/2+peq
        reserved=sum((o.qty*o.price*(1+self.config.spot_maker_fee) for o in self.orders.values() if o.side=='buy'),ZERO)
        account=Account(self.spot_qty,self.perp_qty,equity,self.cash-reserved,max(ZERO,peq-abs(self.perp_qty)*mark))
        return Snapshot(self.clock_ms(),spot,perp,account,tuple(self.orders.values()),tuple(f for f in self.fills if f.time_ms>=since_ms))

    def submit(self, action: Action):
        if action.kind=='cancel':
            oid=action.oid or next((o.oid for o in self.orders.values() if o.cloid==action.cloid),None)
            original=self.orders.pop(oid,None)
            result=Result('canceled',oid)
            if original:
                done=sum((f.qty for f in self.fills if f.oid==oid),ZERO)
                self.results[original.cloid]=Result('canceled',oid,done)
            self.results[action.cloid]=result
            return result
        if action.cloid in self.results:
            return self.results[action.cloid]
        spot,perp=self.books(); book=spot if action.market=='spot' else perp
        if action.qty*action.price<self.config.min_notional_usdc:
            return Result('rejected',error='Below minimum notional')
        self.counter+=1; oid=str(self.counter)
        if action.market=='spot':
            if action.tif!='Alo':
                return Result('rejected',error='Paper spot orders require post-only')
            if (action.side=='buy' and action.price>=book.ask) or (action.side=='sell' and action.price<=book.bid):
                return Result('rejected',error='Post-only price crosses book')
            if action.side=='buy' and action.qty*action.price*(1+self.config.spot_maker_fee)>self.cash:
                return Result('rejected',error='Insufficient spot cash')
            if action.side=='sell' and action.qty>self.spot_qty:
                return Result('rejected',error='Insufficient spot inventory')
            order=OpenOrder(oid,action.cloid,'spot',action.side,action.qty,action.price,self.clock_ms())
            self.orders[oid]=order; result=Result('accepted',oid)
        else:
            if action.tif!='Ioc':
                return Result('rejected',error='Perp hedges require IOC')
            signed=action.qty if action.side=='buy' else -action.qty
            if action.reduce_only and (self.perp_qty*signed>=ZERO or abs(signed)>abs(self.perp_qty)):
                return Result('rejected',error='Reduce-only would increase or flip exposure')
            if abs(self.perp_qty+signed)*action.price>self.perp_cash:
                return Result('rejected',error='Paper 1x margin capacity exceeded')
            remaining=action.qty; done=ZERO
            for level in book.asks if action.side=='buy' else book.bids:
                if (action.side=='buy' and level.price>action.price) or (action.side=='sell' and level.price<action.price):
                    break
                qty=min(remaining,level.qty)
                if qty<=ZERO: continue
                self._fill(oid,'perp',action.side,qty,level.price,f'paper:ioc:{oid}:{done}',self.clock_ms())
                remaining-=qty;done+=qty
                if remaining<=ZERO:break
            result=Result('filled' if done else 'canceled',oid,done)
        self.results[action.cloid]=result
        return result

    def resolve(self, action):
        return self.results.get(action.cloid,Result('unknown',error='No durable paper result'))

    def refresh_deadman(self):
        return None
