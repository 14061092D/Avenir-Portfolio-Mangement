from dataclasses import replace
from decimal import Decimal as D
import json

from avenir_mm.config import Config
from avenir_mm.domain import Account, Book, Fill, Funding, Level, OpenOrder, Precision, Result, Snapshot
from avenir_mm.engine import Engine, EngineState


T=1000000


def snapshot(engine, *, now=T, orders=(), fills=(), funding=(), equity=D('500'), healthy=True,
             spot_qty=None, perp_qty=None, bid=D('100'), ask=D('100.02')):
    st=engine.state
    return Snapshot(now,
        Book('@142',now,now,(Level(bid-D('.02'),D('10')),),(Level(ask,D('10')),)),
        Book('BTC',now,now,(Level(bid,D('10')),),(Level(ask,D('10')),)),
        Account(st.spot_qty if spot_qty is None else spot_qty,
                st.perp_qty if perp_qty is None else perp_qty,equity,D('300'),D('200')),
        tuple(orders),tuple(fills),tuple(funding),healthy)


def engine(**changes):
    return Engine(replace(Config(),**changes),Precision(6,5))


def place_entry(e):
    a=e.step(snapshot(e));assert a and a.side=='buy' and a.market=='spot'
    e.record_result(a,Result('accepted','spot1'))
    order=OpenOrder('spot1',a.cloid,'spot','buy',a.qty,a.price,T)
    assert e.step(snapshot(e,orders=(order,))) is None
    return a


def fill_entry(e,a,qty=None,now=T+100,base_fee=D('0'),order_remaining=None):
    qty=qty if qty is not None else a.qty
    f=Fill('entry','spot','buy',qty,a.price,qty*a.price*D('.0004'),base_fee,'spot1',now)
    orders=() if order_remaining is None else (OpenOrder('spot1',a.cloid,'spot','buy',order_remaining,a.price,T),)
    out=e.step(snapshot(e,now=now,fills=(f,),orders=orders,spot_qty=qty-base_fee))
    return f,out


def test_first_quote_is_buy_only_and_persisted_before_submission():
    e=engine();a=e.step(snapshot(e))
    assert a.market=='spot' and a.side=='buy' and a.tif=='Alo'
    assert a.price<=D('99')
    assert e.state.pending==a
    assert a.cloid.startswith('0xa7e1') and len(a.cloid)==34
    restored=EngineState.from_dict(json.loads(json.dumps(e.state.to_dict())))
    assert restored.pending==a and restored.owned_actions[a.cloid]==a
    assert Engine(e.config,e.precision,restored).step(snapshot(e)) is None


def test_no_modification_for_unchanged_quote():
    e=engine();a=place_entry(e)
    order=OpenOrder('spot1',a.cloid,'spot','buy',a.qty,a.price,T)
    assert e.step(snapshot(e,now=T+120000,orders=(order,))) is None


def test_accepted_ack_with_lagged_empty_orders_cannot_duplicate_maker():
    e=engine();a=e.step(snapshot(e));e.record_result(a,Result('accepted','spot1'))
    assert e.step(snapshot(e,now=T+120000)) is None
    assert e.state.pending==a
    order=OpenOrder('spot1',a.cloid,'spot','buy',a.qty,a.price,T)
    assert e.step(snapshot(e,now=T+120000,orders=(order,))) is None
    assert e.state.pending is None


def test_partial_fill_hedges_actual_quantity_before_canceling_remainder():
    e=engine();a=place_entry(e)
    f,h=fill_entry(e,a,D('.12'),order_remaining=D('.08'))
    assert h.market=='perp' and h.side=='sell' and h.qty==D('.12')
    e.record_result(h,Result('filled','hedge1',D('.12')))
    assert e.state.pending==h  # ACK is not an authoritative fill.
    hf=Fill('hedge','perp','sell',D('.12'),D('100'),D('.0054'),D('0'),'hedge1',T+200)
    order=OpenOrder('spot1',a.cloid,'spot','buy',D('.08'),a.price,T)
    cancel=e.step(snapshot(e,now=T+200,orders=(order,),fills=(f,hf),perp_qty=D('-.12')))
    assert cancel.kind=='cancel' and cancel.oid=='spot1'


def test_duplicate_fill_does_not_double_inventory_or_hedge():
    e=engine();a=place_entry(e);f,h=fill_entry(e,a)
    assert e.state.spot_qty==a.qty
    assert e.step(snapshot(e,now=T+200,fills=(f,))) is None
    assert e.state.spot_qty==a.qty and e.state.pending==h


def test_partial_ioc_waits_for_actual_fill_then_hedges_only_residual():
    e=engine(order_notional_usdc=D('40'),max_unhedged_usdc=D('50'))
    a=place_entry(e);ef,h=fill_entry(e,a)
    e.record_result(h,Result('filled','hedge1',D('.2')))
    assert e.step(snapshot(e,now=T+150,fills=(ef,))) is None
    hf=Fill('partialhedge','perp','sell',D('.2'),D('100'),D('.009'),D('0'),'hedge1',T+200)
    remainder=e.step(snapshot(e,now=T+200,fills=(ef,hf),perp_qty=D('-.2')))
    assert remainder.market=='perp' and remainder.qty==D('.2')
    assert remainder.cloid!=h.cloid
    assert e.state.spot_qty==D('.4') and e.state.perp_qty==D('-.2')


def test_sub_quantum_rounding_residual_does_not_deadlock_exit():
    e=engine();a=place_entry(e);ef,h=fill_entry(e,a,base_fee=D('.000001'))
    e.record_result(h,Result('filled','hedge1',h.qty))
    hf=Fill('hedge','perp','sell',h.qty,D('100'),D('.009'),D('0'),'hedge1',T+200)
    out=e.step(snapshot(e,now=T+200,fills=(ef,hf),perp_qty=-h.qty))
    assert out.market=='spot' and out.side=='sell'
    assert out.qty==a.qty-D('.000001')
    assert not e.state.halted_reason


def test_base_fee_reduces_spot_delta_without_double_charging_cash():
    e=engine();a=place_entry(e)
    f=Fill('basefee','spot','buy',a.qty,a.price,D('0'),D('.000001'),'spot1',T+100)
    h=e.step(snapshot(e,now=T+100,fills=(f,),spot_qty=a.qty-D('.000001')))
    assert e.state.spot_qty==a.qty-D('.000001')
    assert e.state.cycle_cashflow==-a.qty*a.price
    assert h.qty==e.precision.floor_qty(a.qty-D('.000001'),'perp')


def test_unknown_ack_survives_restart_and_does_not_resubmit():
    e=engine();a=e.step(snapshot(e));e.record_result(a,Result('unknown',error='timeout'))
    restored=Engine(e.config,e.precision,e.state.to_dict())
    assert restored.step(snapshot(restored,now=T+10000)) is None
    assert restored.state.pending==a


def test_fill_before_ack_is_deferred_until_order_identity_resolved():
    e=engine();a=e.step(snapshot(e))
    f=Fill('early','spot','buy',a.qty,a.price,D('0'),D('0'),'spot1',T+100)
    assert e.step(snapshot(e,now=T+100,fills=(f,),spot_qty=a.qty)) is None
    assert f.key not in e.state.seen_fills
    e.record_result(a,Result('filled','spot1',a.qty))
    h=e.step(snapshot(e,now=T+200,fills=(f,),spot_qty=a.qty))
    assert h.market=='perp' and e.state.spot_qty==a.qty


def test_late_fill_after_cancel_ack_still_hedged():
    e=engine();a=place_entry(e)
    order=OpenOrder('spot1',a.cloid,'spot','buy',a.qty,a.price,T)
    cancel=e.step(snapshot(e,now=T+100,orders=(order,),bid=D('99.5'),ask=D('99.52')))
    assert cancel.kind=='cancel'
    e.record_result(cancel,Result('canceled','spot1'))
    f,h=fill_entry(e,a,now=T+200)
    assert h.market=='perp' and h.qty==a.qty


def test_small_partial_fill_is_retained_then_halts_on_timeout():
    e=engine();a=place_entry(e)
    f,out=fill_entry(e,a,D('.05'),order_remaining=D('.15'))
    assert out is None and e.state.dust_since_ms
    order=OpenOrder('spot1',a.cloid,'spot','buy',D('.15'),a.price,T)
    out=e.step(snapshot(e,now=T+10200,orders=(order,),fills=(f,)))
    assert out.kind=='cancel' and e.state.halted_reason=='unhedgeable_residual_timeout'


def test_stale_books_cancel_existing_maker():
    e=engine();a=place_entry(e)
    order=OpenOrder('spot1',a.cloid,'spot','buy',a.qty,a.price,T)
    s=snapshot(e,now=T+3000,orders=(order,))
    s=replace(s,perp=replace(s.perp,time_ms=T))
    out=e.step(s)
    assert out.kind=='cancel' and e.state.halted_reason=='stale_or_unhealthy_data'


def test_unexplained_account_position_does_not_trigger_speculative_hedge():
    e=engine()
    assert e.step(snapshot(e,spot_qty=D('.1'))) is None
    assert e.state.halted_reason=='account_reconciliation_required'


def test_losing_account_stops_new_entries():
    e=engine();e.state.started_ms=T;e.state.overall_equity_peak=D('500')
    e.state.equity_history=[(T,D('500'))]
    assert e.step(snapshot(e,now=T+60000,equity=D('494'))) is None
    assert e.state.halted_reason=='rolling_24h_loss_limit'


def test_repeated_rejected_hedges_stop_retrying_and_cancel_maker():
    e=engine();a=place_entry(e);f,h=fill_entry(e,a,D('.12'),order_remaining=D('.08'))
    o=OpenOrder('spot1',a.cloid,'spot','buy',D('.08'),a.price,T)
    for i in range(3):
        e.record_result(h,Result('rejected',error='rejected'))
        h=e.step(snapshot(e,now=T+200+i,orders=(o,),fills=(f,)))
    assert h.kind=='cancel' and e.state.halted_reason=='hedge_rejections_exhausted'


def test_complete_cycle_uses_maker_exit_and_reduce_only_taker_close():
    e=engine();entry=place_entry(e);ef,h=fill_entry(e,entry)
    e.record_result(h,Result('filled','hedge1',h.qty))
    hf=Fill('hedge','perp','sell',h.qty,D('100'),h.qty*D('100')*D('.00045'),D('0'),'hedge1',T+200)
    exit_order=e.step(snapshot(e,now=T+200,fills=(ef,hf),perp_qty=-h.qty))
    assert exit_order.side=='sell' and exit_order.market=='spot' and exit_order.tif=='Alo'
    assert exit_order.qty==entry.qty
    e.record_result(exit_order,Result('accepted','exit1'))
    xf=Fill('exit','spot','sell',exit_order.qty,exit_order.price,exit_order.qty*exit_order.price*D('.0004'),D('0'),'exit1',T+300)
    close=e.step(snapshot(e,now=T+300,fills=(ef,hf,xf),spot_qty=D('0')))
    assert close.market=='perp' and close.side=='buy' and close.reduce_only
    assert close.price<=D('100.02')*(1+e.config.slippage_bp/10000)
    e.record_result(close,Result('filled','close1',close.qty))
    cf=Fill('close','perp','buy',close.qty,D('100.02'),close.qty*D('100.02')*D('.00045'),D('0'),'close1',T+400)
    e.step(snapshot(e,now=T+400,fills=(ef,hf,xf,cf),perp_qty=D('0')))
    assert e.state.completed_cycles==1 and e.state.spot_qty==e.state.perp_qty==0
    assert e.state.realized_pnl>=entry.qty*entry.price*e.config.minimum_profit_bp/10000
    assert e.state.halted_reason==''


def test_entry_reserves_spot_fee_in_available_cash():
    e=engine();s=snapshot(e)
    # The order notional is exactly 19.8; that balance cannot also pay its fee.
    s=replace(s,account=replace(s.account,spot_cash=D('19.8')))
    assert e.step(s) is None
    assert e.state.pending is None


def test_entry_reserves_hedge_fee_and_slippage_in_margin_cash():
    e=engine();s=snapshot(e)
    s=replace(s,account=replace(s.account,perp_withdrawable=D('20')))
    assert e.step(s) is None
    assert e.state.pending is None


def test_maximum_holding_time_freezes_matched_position_without_claiming_flatten():
    e=engine(max_holding_ms=1000);a=place_entry(e);ef,h=fill_entry(e,a)
    e.record_result(h,Result('filled','hedge1',h.qty))
    hf=Fill('hedge','perp','sell',h.qty,D('100'),D('0'),D('0'),'hedge1',T+200)
    out=e.step(snapshot(e,now=T+2000,fills=(ef,hf),perp_qty=-h.qty))
    assert out is None and e.state.halted_reason=='maximum_holding_time'
    assert e.state.spot_qty>0 and e.state.perp_qty<0


def test_funding_deduplicates_in_cycle_cashflow():
    e=engine();a=place_entry(e);ef,h=fill_entry(e,a)
    before=e.state.cycle_cashflow
    f=Funding('f1',D('.01'),T+200)
    e.step(snapshot(e,now=T+200,funding=(f,f)))
    assert e.state.cycle_cashflow==before+D('.01')
