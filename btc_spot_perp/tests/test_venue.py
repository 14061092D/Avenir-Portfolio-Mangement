from dataclasses import replace
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from avenir_mm.config import Config
from avenir_mm.domain import Action, Book, Level
from avenir_mm.venue import LiveVenue, PairMetadata, VenueError, RateBudgetExceeded, SubmissionNotSent, WeightBudget, discover_pair, parse_book, UBTC_TOKEN_ID


ADDRESS='0x'+'1'*40
CLOID='0xa7e1'+'2'*28


class FakeInfo:
    def __init__(self):
        self.calls=[]; self.abstraction='disabled'; self.orders=[]; self.fills=[]
        self.resolve={'status':'unknownOid'}
        self.spot={'balances':[{'coin':'USDC','token':0,'total':'150','hold':'0'},
                               {'coin':'UBTC','token':197,'total':'0','hold':'0'}]}
        self.perp={'assetPositions':[],'marginSummary':{'accountValue':'350'},'withdrawable':'350'}
    def spot_meta(self):
        return {'tokens':[{'index':0,'name':'USDC','szDecimals':8},
                          {'index':197,'name':'UBTC','tokenId':UBTC_TOKEN_ID,'szDecimals':5}],
                'universe':[{'index':142,'name':'@142','tokens':[197,0]}]}
    def meta(self): return {'universe':[{'name':'BTC','szDecimals':5}]}
    def post(self,path,payload):
        self.calls.append((payload['type'],payload['user']))
        if payload['type']=='userAbstraction': return self.abstraction
        if payload['type']=='userRole': return {'role':'user'}
        raise AssertionError(payload)
    def user_rate_limit(self,addr):
        self.calls.append(('rate',addr));return {'nRequestsCap':10000,'nRequestsUsed':10}
    def spot_user_state(self,addr): self.calls.append(('spot',addr));return self.spot
    def user_state(self,addr): self.calls.append(('perp',addr));return self.perp
    def open_orders(self,addr): self.calls.append(('orders',addr));return self.orders
    def user_fills_by_time(self,addr,start,end,aggregate):
        self.calls.append(('fills',addr));return self.fills
    def user_funding_history(self,addr,start,end):
        self.calls.append(('funding',addr));return []
    def query_order_by_cloid(self,addr,cloid):
        self.calls.append(('resolve',addr));return self.resolve


class FakeExchange:
    def __init__(self): self.calls=[];self.reply={'status':'ok','response':{'data':{'statuses':[{'resting':{'oid':42}}]}}};self.error=None
    def set_expires_after(self,t): self.expires=t
    def schedule_cancel(self,t):
        self.calls.append(('deadman',t));return {'status':'ok','response':{'type':'default'}}
    def order(self,*args,**kwargs):
        self.calls.append(('order',args,kwargs))
        if self.error: raise self.error
        return self.reply
    def cancel_by_cloid(self,*args):
        self.calls.append(('cancel',args));return self.reply


@pytest.fixture
def setup():
    now=[1_800_000_000_000]
    info,ex=FakeInfo(),FakeExchange();pair=discover_pair(info)
    def books():
        return tuple(Book(c,now[0],now[0],(Level(D('74000'),D('1')),),(Level(D('74001'),D('1')),)) for c in ['@142','BTC'])
    venue=LiveVenue(Config(mode='live'),pair,info,ex,ADDRESS,books,clock_ms=lambda:now[0])
    return venue,info,ex,now


def action(**changes):
    return replace(Action('place','spot','buy',D('.00027'),D('73260'),CLOID),**changes)


def arm(v):
    v.preflight(1_799_999_990_000)
    assert v.refresh_deadman(force=True).status=='accepted'


def test_metadata_uses_token_identity_and_dynamic_pair_index():
    info=FakeInfo();original=info.spot_meta
    def changed():
        m=original();m['universe'][0].update(index=999,name='@999');return m
    info.spot_meta=changed
    assert discover_pair(info).spot_coin=='@999'


def test_metadata_rejects_lookalike():
    info=FakeInfo();original=info.spot_meta
    def changed():
        m=original();m['tokens'][1]['tokenId']='0x'+'f'*32;return m
    info.spot_meta=changed
    with pytest.raises(VenueError,match='identity'):discover_pair(info)


def test_public_book_boundary_rejects_crossed_book():
    with pytest.raises(VenueError,match='Crossed'):
        parse_book({'coin':'BTC','time':1,'levels':[[{'px':'100','sz':'1'}],[{'px':'99','sz':'1'}]]},1)


def test_preflight_is_read_only_and_uses_trading_address(setup):
    v,info,ex,_=setup
    snapshot=v.preflight(1)
    assert snapshot.account.equity==D('500')
    assert not ex.calls
    assert all(addr==ADDRESS for _,addr in info.calls)


@pytest.mark.parametrize('mode',['default','unifiedAccount','portfolioMargin','dexAbstraction',None])
def test_unknown_or_unified_account_fails_closed(setup,mode):
    v,info,ex,_=setup;info.abstraction=mode
    with pytest.raises(VenueError,match='abstraction'):v.preflight(1)
    assert not ex.calls


def test_shadow_guard_precedes_environment_key_read(monkeypatch,setup):
    v,_,_,_=setup
    monkeypatch.setenv('HL_AGENT_PRIVATE_KEY','not-a-key')
    with pytest.raises(VenueError,match='operator'):
        LiveVenue.from_env(Config(),v.pair,v.books,operator_confirmed=False)


def test_spot_order_exact_sdk_payload_and_expiry(setup):
    v,_,ex,now=setup;arm(v)
    result=v.submit(action())
    assert result.status=='accepted' and result.oid=='42'
    _,args,kw=ex.calls[-1]
    assert args==('@142',True,.00027,73260.0,{'limit':{'tif':'Alo'}})
    assert kw['reduce_only'] is False and kw['cloid'].to_raw()==CLOID
    assert ex.expires==now[0]+5000


def test_hedge_uses_bounded_ioc_and_exit_reduce_only(setup):
    v,_,ex,_=setup;arm(v)
    v.submit(action(market='perp',side='buy',price=D('74050'),tif='Ioc',reduce_only=True))
    _,args,kw=ex.calls[-1]
    assert args[0]=='BTC' and args[4]=={'limit':{'tif':'Ioc'}}
    assert kw['reduce_only'] is True
    with pytest.raises(VenueError,match='slippage'):
        v._validate_action(action(market='perp',side='sell',price=D('73000'),tif='Ioc'))


def test_timeout_is_unknown_and_cannot_be_resubmitted(setup):
    v,_,ex,_=setup;arm(v);ex.error=TimeoutError('network timeout')
    assert v.submit(action()).status=='unknown'
    assert v.submit(action()).status=='unknown'
    assert sum(c[0]=='order' for c in ex.calls)==1


def test_filled_ack_not_a_ledger_fill_and_partial_quantity_retained(setup):
    v,_,ex,_=setup;arm(v)
    ex.reply={'status':'ok','response':{'data':{'statuses':[{'filled':{'oid':42,'totalSz':'.00014','avgPx':'74000'}}]}}}
    r=v.submit(action(market='perp',side='sell',price=D('73950'),tif='Ioc'))
    assert r.status=='filled' and r.filled_qty==D('.00014')
    assert not v._cache.fills


def test_malformed_http_ok_is_unknown(setup):
    v,_,ex,_=setup;arm(v);ex.reply={'status':'ok','response':{}}
    assert v.submit(action()).status=='unknown'


def test_cancel_error_requires_resolution(setup):
    v,_,ex,_=setup;arm(v);ex.reply={'status':'ok','response':{'data':{'statuses':[{'error':'already filled'}]}}}
    v._oid_cloid['42']=CLOID
    assert v.submit(action(kind='cancel',oid='42')).status=='unknown'


def test_fill_base_fee_and_global_dedup_key(setup):
    v,info,_,now=setup
    raw={'coin':'@142','side':'B','px':'74000','sz':'.0002','fee':'.00000008','feeToken':'UBTC','oid':42,'time':now[0]-1,'tid':7}
    fills=v._parse_fills([raw,raw,{**raw,'time':now[0]}])
    assert len(fills)==2 and fills[0].base_fee==D('.00000008') and fills[0].fee_usdc==0
    with pytest.raises(VenueError,match='fee token'):
        v._parse_fills([{**raw,'feeToken':'HYPE'}])


def test_unknown_order_not_assumed_rejected(setup):
    v,_,_,_=setup
    assert v.resolve(action()).status=='unknown'


def test_restart_resolves_owned_order_without_cloid(setup):
    v,info,_,now=setup
    v.register_actions([action()])
    info.resolve={'status':'order','order':{'status':'open','order':{'coin':'@142','oid':42,'origSz':'.00027'}}}
    info.orders=[{'coin':'@142','oid':42,'side':'B','sz':'.00027','limitPx':'73260','timestamp':now[0]-100}]
    snap=v.preflight(1)
    assert snap.orders[0].cloid==CLOID and not snap.account.foreign_activity


def test_address_reserve_blocks_entries_but_allows_cancel(setup):
    v,_,ex,_=setup;arm(v);v._remaining=1000
    with pytest.raises(SubmissionNotSent,match='reserve'):v.submit(action())
    ex.reply={'status':'ok','response':{'data':{'statuses':['success']}}}
    v._oid_cloid['42']=CLOID
    assert v.submit(action(kind='cancel',oid='42')).status=='canceled'


def test_no_small_hedge_roundup(setup):
    v,_,_,_=setup
    with pytest.raises(VenueError,match='minimum'):
        v._validate_action(action(market='perp',qty=D('.0001'),price=D('74000'),side='sell',tif='Ioc'))


def test_full_snapshot_cache_and_forced_fill_refresh(setup):
    v,info,_,now=setup
    v.snapshot(1);reads=len(info.calls)
    now[0]+=100
    v.snapshot(1);assert len(info.calls)==reads
    info.spot['balances'][1]['total']='.0002'
    snap=v.snapshot(1,force=True)
    assert snap.account.spot_qty==D('.0002') and len(info.calls)>reads


def test_local_budget_window():
    now=[1000];b=WeightBudget(3,lambda:now[0]);b.consume(3)
    with pytest.raises(RateBudgetExceeded):b.consume(1)
    now[0]+=60000;b.consume(3)


def test_cancel_uses_original_order_id_not_new_intent_id(setup):
    v,_,ex,_=setup;arm(v);v.submit(action())
    target=CLOID;intent='0xa7e1'+'3'*28
    ex.reply={'status':'ok','response':{'data':{'statuses':['success']}}}
    assert v.submit(action(kind='cancel',cloid=intent,oid='42')).status=='canceled'
    assert ex.calls[-1][1][1].to_raw()==target
    assert v._oid_cloid['42']==target


def test_rest_reserve_keeps_room_for_safety_action():
    b=WeightBudget(300,lambda:1000,emergency_reserve=50)
    b.consume(250)
    with pytest.raises(RateBudgetExceeded):b.consume(1)
    b.consume(1,critical=True)


def test_restart_ownership_avoids_queries_for_all_completed_orders(setup):
    v,info,_,_=setup
    actions=[action(cloid='0xa7e1'+f'{i:028x}') for i in range(200)]
    v.register_actions(actions,{str(i):a.cloid for i,a in enumerate(actions)})
    v.preflight(1)
    assert not any(kind=='resolve' for kind,_ in info.calls)


def test_concurrent_balance_change_during_read_is_not_authoritative(setup):
    v,info,_,_=setup
    original=info.spot_user_state;calls=[0]
    def changing(addr):
        import copy
        calls[0]+=1
        value=copy.deepcopy(original(addr))
        if calls[0]>1:value['balances'][1]['total']='.0001'
        return value
    info.spot_user_state=changing
    with pytest.raises(VenueError,match='changed during reconciliation'):
        v.snapshot(1,force=True)


def test_local_rejection_does_not_create_unknown_network_intent(setup):
    v,_,ex,_=setup;arm(v)
    invalid=action(qty=D('.00001'))
    with pytest.raises(SubmissionNotSent,match='not_sent:.*minimum'):
        v.submit(invalid)
    assert not any(c[0]=='order' for c in ex.calls)
    assert 'place:'+invalid.cloid not in v._sent
    # A durable controller can record a rejection; no order-status lookup is needed.


def test_expiry_setter_failure_is_known_not_sent(setup):
    v,_,ex,_=setup;arm(v)
    def broken(_):raise ValueError('local expiry validation failed')
    ex.set_expires_after=broken
    with pytest.raises(SubmissionNotSent,match='not_sent'):
        v.submit(action())
    assert not any(c[0]=='order' for c in ex.calls)


def test_installed_sdk_order_and_cancel_contract_without_network():
    import importlib.metadata
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from hyperliquid.utils.types import Cloid
    assert importlib.metadata.version('hyperliquid-python-sdk')=='0.24.0'
    exchange=Exchange.__new__(Exchange)
    calls=[]
    exchange.bulk_orders=lambda orders,builder=None:calls.append(('orders',orders,builder)) or {'ok':True}
    exchange.bulk_cancel_by_cloid=lambda cancels:calls.append(('cancel',cancels)) or {'ok':True}
    cloid=Cloid.from_str(CLOID)
    exchange.order('BTC',False,.00027,73950,{'limit':{'tif':'Ioc'}},reduce_only=False,cloid=cloid)
    request=calls[-1][1][0]
    assert request['coin']=='BTC' and request['order_type']=={'limit':{'tif':'Ioc'}}
    assert request['cloid'].to_raw()==CLOID
    exchange.cancel_by_cloid('@142',cloid)
    assert calls[-1][1][0]['coin']=='@142'
    info=Info.__new__(Info);info.post=lambda path,payload:(path,payload)
    assert info.query_order_by_cloid(ADDRESS,cloid)==('/info',{'type':'orderStatus','user':ADDRESS,'oid':CLOID})
    assert info.user_rate_limit(ADDRESS)[1]['type']=='userRateLimit'
    assert info.user_fills_by_time(ADDRESS,1,None,False)[1]['aggregateByTime'] is False
    assert info.user_funding_history(ADDRESS,1,2)[1]['endTime']==2


def test_wrong_resolved_side_never_becomes_confirmed_fill(setup):
    v,info,_,_=setup
    info.resolve={'status':'order','order':{'status':'filled','order':{'coin':'@142','oid':42,'origSz':'.00027','side':'A'}}}
    assert v.resolve(action(side='buy')).status=='unknown'


def test_invalid_open_order_schema_fails_closed(setup):
    v,info,_,now=setup
    v.register_actions([action()],{'42':CLOID})
    info.orders=[{'coin':'@142','oid':42,'side':'X','sz':'.00027','limitPx':'73260','timestamp':now[0]}]
    with pytest.raises(VenueError,match='Invalid open-order'):
        v.snapshot(1)


def test_cancel_resolution_with_open_target_is_not_accepted(setup):
    v,info,_,_=setup
    v.register_actions([action()],{'42':CLOID})
    info.resolve={'status':'order','order':{'status':'open','order':{'coin':'@142','oid':42,'origSz':'.00027','side':'B'}}}
    result=v.resolve(action(kind='cancel',cloid='0xa7e1'+'3'*28,oid='42'))
    assert result.status=='unknown' and 'still pending' in result.error
    assert v._oid_cloid['42']==CLOID
