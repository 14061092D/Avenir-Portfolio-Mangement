from decimal import Decimal as D
import pytest

from avenir_mm.config import Config
from avenir_mm.domain import Book, Level, Precision
from avenir_mm.engine import profitable_exit_price


def test_exit_formula_includes_all_paid_and_future_fees():
    qty=D('0.2');s0=D('99');f0=D('100');f1=D('101')
    paid=qty*(s0*D('.0004')+f0*D('.00045'))
    cash=qty*(f0-s0)-paid
    target=D('.1')
    exit_px=profitable_exit_price(cash,target,qty,qty,f1,D('.0004'),D('.00045'))
    actual=cash+qty*exit_px*(1-D('.0004'))-qty*f1*(1+D('.00045'))
    assert abs(actual-target)<D('1e-24')


def test_exit_formula_handles_base_fee_and_unequal_remaining_legs():
    spot_qty=D('.19999');perp_qty=D('.1999');cash=D('.12');target=D('.05')
    price=profitable_exit_price(cash,target,spot_qty,perp_qty,D('101'),D('.0004'),D('.00045'))
    pnl=cash+spot_qty*price*(1-D('.0004'))-perp_qty*D('101')*(1+D('.00045'))
    assert abs(pnl-target)<D('1e-24')


def test_depth_vwap_is_quantity_aware_and_insufficient_depth_is_unknown():
    b=Book('BTC',1,1,(Level(D('100'),D('1')),Level(D('99'),D('2'))),(Level(D('101'),D('1')),))
    assert b.vwap('sell',D('2'))==D('99.5')
    assert b.vwap('buy',D('2')) is None


def test_btc_precision_respects_perp_size_and_valid_prices():
    p=Precision(8,5)
    assert p.floor_qty(D('.000269999'),'perp')==D('.00026')
    assert p.round_price(D('74235.99'),'perp')==D('74235')
    assert p.round_price(D('74235.01'),'spot',up=True)==D('74236')


@pytest.mark.parametrize('values',[
    {'capital_usdc':'NaN'}, {'order_notional_usdc':'9'},
    {'max_inventory_usdc':'500'}, {'mode':'maybe'},
    {'entry_offset_bp':'80'}, {'perp_coin':'ETH'},
    {'spot_maker_fee':'1'}, {'max_book_age_ms':0}, {'unknown':1},
])
def test_invalid_config_is_rejected_at_boundary(values):
    with pytest.raises(ValueError):Config.from_dict(values)


def test_config_serialization_preserves_decimal_settings():
    cfg=Config()
    assert Config.from_dict(cfg.to_dict())==cfg
