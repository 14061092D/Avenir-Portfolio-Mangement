from types import SimpleNamespace
from avenir_mm.config import Config
from avenir_mm.domain import Action,Book,D,Level,Precision
from avenir_mm.paper import PaperVenue
from avenir_mm.cli import demo


def setup():
    clock=[1000]
    def books():
        t=clock[0]
        return Book('@142',t,t,(Level(D('100'),D('1')),),(Level(D('101'),D('1')),)),Book('BTC',t,t,(Level(D('100'),D('.1')),),(Level(D('101'),D('.1')),))
    pair=SimpleNamespace(spot_coin='@142',precision=Precision(6,5))
    return clock,PaperVenue(Config(),pair,books,clock_ms=lambda:clock[0],participation=D('1'))


def test_synthetic_cycle_returns_flat(capsys):
    assert demo()==0
    assert '"synthetic":true' in capsys.readouterr().out


def test_touch_and_old_public_prints_do_not_fill_and_duplicates_do_not_double_fill():
    clock,v=setup()
    a=Action('place','spot','buy',D('.2'),D('99'),'0x'+'1'*32)
    assert v.submit(a).status=='accepted'
    trade={'time':1001,'coin':'@142','tid':1,'side':'A','px':'98','sz':'.1'}
    v.observe_trade(trade);assert v.spot_qty==0
    trade.update(time=1500,px='99');v.observe_trade(trade);assert v.spot_qty==0
    trade.update(tid=2,px='98');v.observe_trade(trade);v.observe_trade(trade)
    assert v.spot_qty==D('.1') and len(v.fills)==1


def test_ioc_partial_depth_and_reduce_only():
    _,v=setup()
    a=Action('place','perp','sell',D('.2'),D('99'),'0x'+'2'*32,tif='Ioc')
    r=v.submit(a)
    assert r.filled_qty==D('.1') and v.perp_qty==D('-.1')
    bad=Action('place','perp','buy',D('.2'),D('102'),'0x'+'3'*32,tif='Ioc',reduce_only=True)
    assert v.submit(bad).status=='rejected'


def test_shadow_account_survives_serialization():
    import json
    from avenir_mm.store import encode
    clock,v=setup()
    v.submit(Action('place','perp','sell',D('.1'),D('99'),'0x'+'4'*32,tif='Ioc'))
    restored=PaperVenue(v.config,v.pair,v.books,json.loads(encode(v.to_dict())),clock_ms=lambda:clock[0])
    assert restored.snapshot(0).account==v.snapshot(0).account


def test_cancel_keeps_original_order_resolvable_without_reopening():
    _,v=setup()
    original=Action('place','spot','buy',D('.2'),D('99'),'0x'+'5'*32)
    placed=v.submit(original)
    cancel=Action('cancel','spot','buy',D('.2'),D('99'),'0x'+'6'*32,oid=placed.oid)
    assert v.submit(cancel).status=='canceled'
    assert v.resolve(original).status=='canceled'
    assert not v.orders
