"""Strict JSON conversion at feed and configuration boundaries."""

from dataclasses import fields
from decimal import Decimal
from .models import (
    Instrument,
    Pair,
    Book,
    BasisSample,
    FundingPoint,
    Market,
    SignalConfig,
    EngineConfig,
    dec,
)


def instrument(raw):
    value = dict(raw)
    for key in ("tick_size", "qty_step", "min_qty", "min_notional", "contract_size"):
        if key in value:
            value[key] = dec(value[key])
    return Instrument(**value)


def pair(raw):
    value = dict(raw)
    value["maker"] = instrument(value["maker"])
    value["hedge"] = instrument(value["hedge"])
    for key in (
        "maker_fee_bps",
        "hedge_fee_bps",
        "target_notional",
        "hedge_margin_fraction",
    ):
        if key in value and value[key] is not None:
            value[key] = dec(value[key])
    return Pair(**value)


def config(raw):
    value = dict(raw)
    nested = dict(value.pop("signal", {}))
    for field in fields(SignalConfig):
        if field.name in nested and isinstance(field.default, Decimal):
            nested[field.name] = dec(nested[field.name])
    for field in fields(EngineConfig):
        if field.name in value and isinstance(field.default, Decimal):
            value[field.name] = dec(value[field.name])
    value["signal"] = SignalConfig(**nested)
    return EngineConfig(**value)


def book(raw):
    return Book(
        tuple((dec(p), dec(q)) for p, q in raw["bids"]),
        tuple((dec(p), dec(q)) for p, q in raw["asks"]),
        raw["received_ms"],
    )


def market(raw):
    return Market(
        raw["pair_id"],
        book(raw["maker_book"]),
        book(raw["hedge_book"]),
        tuple(
            BasisSample(r["timestamp_ms"], dec(r["basis_bps"]))
            for r in raw["basis_history"]
        ),
        tuple(
            FundingPoint(
                r["timestamp_ms"],
                dec(r["rate"]),
                r["interval_ms"],
                r.get("kind", "regular"),
            )
            for r in raw["funding"]
        ),
    )
