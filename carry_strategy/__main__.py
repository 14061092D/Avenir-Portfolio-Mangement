"""CLI for discovery, public screening and durable event replay."""

import argparse
from dataclasses import asdict
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import time

from .engine import Engine, encode
from .models import (
    Instrument,
    Pair,
    Book,
    BasisSample,
    FundingPoint,
    Market,
    EngineConfig,
    SignalConfig,
)
from .public import PublicClient
from .signal import evaluate
from . import serde


def load_config(path):
    raw = json.loads(Path(path).read_text())
    return [serde.pair(p) for p in raw["pairs"]], serde.config(raw.get("engine", {}))


def emit(value, path=None):
    payload = encode(value) + "\n"
    if path:
        Path(path).write_text(payload)
    else:
        print(payload, end="")


def apply_event(engine, event):
    kind = event["type"]
    if kind == "market":
        return engine.on_market(serde.market(event["market"]), event["now_ms"])
    if kind == "tick":
        return engine.tick(event["now_ms"])
    if kind == "reconcile":
        return engine.reconcile(
            event["positions"],
            event["orders"],
            event["available_quote"],
            event["now_ms"],
        )
    if kind == "fill":
        return engine.on_fill(**{k: v for k, v in event.items() if k != "type"})
    if kind == "order":
        return engine.on_order_update(
            event["client_order_id"],
            event["status"],
            event["cumulative_qty_base"],
            event["now_ms"],
        )
    if kind == "dispatch":
        return engine.mark_dispatched(event["command_id"], event["now_ms"])
    if kind == "exit":
        return engine.request_exit(
            event["pair_id"], event["now_ms"], event.get("reason", "operator_exit")
        )
    if kind == "resume":
        return engine.resume_pair(event["pair_id"])
    raise ValueError(f"Unknown event type {kind}")


def demo(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    db = directory / "state.sqlite"
    if db.exists():
        raise ValueError("Demo directory already contains a database")
    now = 40 * 86400000 + 1000
    spot = Instrument(
        "spot_demo",
        "GOLDUSDT",
        "spot",
        "GOLD",
        "USDT",
        D(".01"),
        D(".0001"),
        D(".0001"),
        D("5"),
    )
    hedge = Instrument(
        "hedge_demo",
        "GOLDUSDT",
        "linear",
        "GOLD",
        "USDT",
        D(".01"),
        D(".001"),
        D(".001"),
        D("5"),
    )
    pair = Pair("demo_gold", spot, hedge, D("1"), D("1"), mapping_verified=True)
    settings = EngineConfig(signal=SignalConfig(fair_window=3, deviation_window=5))
    emit(dict(pairs=[pair], engine=settings), directory / "config.json")
    history = tuple(BasisSample(h * 3600000, D("-5")) for h in range(937, 961))
    funding = tuple(
        FundingPoint(day * 86400000, D(".0003"), 86400000) for day in range(1, 41)
    )
    market = Market(
        pair.pair_id,
        Book(((D("100"), D("100")),), ((D("100.01"), D("100")),), now),
        Book(((D("99.99"), D("100")),), ((D("100"), D("100")),), now),
        history,
        funding,
    )
    engine = Engine(db, [pair], settings)
    events = []

    def send(event):
        events.append(event)
        apply_event(engine, event)

    send(
        dict(
            type="reconcile",
            positions={"demo_gold": dict(spot="0", short="0")},
            orders={},
            available_quote={"spot_demo": "100000", "hedge_demo": "100000"},
            now_ms=now,
        )
    )
    send(dict(type="market", market=asdict(market), now_ms=now))
    maker = next(c for c in engine.pending_commands() if c["kind"] == "place")
    send(dict(type="dispatch", command_id=maker["command_id"], now_ms=now + 1))
    send(
        dict(
            type="order",
            client_order_id=maker["client_order_id"],
            status="open",
            cumulative_qty_base="0",
            now_ms=now + 2,
        )
    )
    qty = D(maker["qty_base"]) / 2
    fill = dict(
        type="fill",
        venue="spot_demo",
        execution_id="spot-fill-1",
        client_order_id=maker["client_order_id"],
        qty_base=str(qty),
        price="100",
        fee_amount=str(qty * D(".0001")),
        fee_currency="base",
        time_ms=now + 10,
    )
    send(fill)
    send(fill)
    short = next(
        c
        for c in engine.pending_commands()
        if c["kind"] == "place" and c["venue"] == "hedge_demo"
    )
    send(dict(type="dispatch", command_id=short["command_id"], now_ms=now + 11))
    send(
        dict(
            type="fill",
            venue="hedge_demo",
            execution_id="hedge-fill-1",
            client_order_id=short["client_order_id"],
            qty_base=short["qty_base"],
            price="99.99",
            fee_amount=str(D(short["qty_base"]) * D("99.99") * D(".0001")),
            fee_currency="quote",
            time_ms=now + 12,
        )
    )
    send(
        dict(
            type="order",
            client_order_id=maker["client_order_id"],
            status="canceled",
            cumulative_qty_base=str(qty),
            now_ms=now + 13,
        )
    )
    state = engine.view()
    assert D(state["pairs"]["demo_gold"]["spot"]) == qty * D(".9999")
    assert D(state["pairs"]["demo_gold"]["short"]) == D(short["qty_base"])
    engine.close()
    (directory / "events.jsonl").write_text("".join(encode(e) + "\n" for e in events))
    emit(
        dict(
            source="synthetic execution demonstration",
            positions=state["pairs"],
            event_count=len(events),
        ),
        directory / "result.json",
    )
    print(f"Demo verified. Artifacts in {directory.resolve()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    d = commands.add_parser("discover")
    d.add_argument("--venue", required=True, choices=["binance", "bitget", "okx"])
    d.add_argument("--maker-fee-bps")
    d.add_argument("--hedge-fee-bps")
    d.add_argument("--mappings")
    d.add_argument("--output")
    s = commands.add_parser("scan")
    s.add_argument("--config", required=True)
    s.add_argument("--output")
    r = commands.add_parser("replay")
    r.add_argument("--config", required=True)
    r.add_argument("--db", required=True)
    r.add_argument("--events", default="-")
    r.add_argument("--output")
    x = commands.add_parser("demo")
    x.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    if args.command == "demo":
        demo(args.output_dir)
        return
    if args.command == "discover":
        client = PublicClient()
        mappings = (
            json.loads(Path(args.mappings).read_text()) if args.mappings else None
        )
        pairs = client.discover_pairs(
            args.venue,
            dict(maker_bps=args.maker_fee_bps, hedge_bps=args.hedge_fee_bps),
            mappings,
        )
        emit(dict(pairs=pairs, engine=EngineConfig()), args.output)
        return
    pairs, settings = load_config(args.config)
    if args.command == "scan":
        client = PublicClient()
        rows = []
        for pair in pairs:
            try:
                data = client.market(pair, int(time.time() * 1000))
                decision = evaluate(
                    pair,
                    data,
                    settings.signal,
                    int(time.time() * 1000),
                    pair.target_notional,
                    settings.capital_limit,
                )
                rows.append(dict(pair_id=pair.pair_id, decision=decision))
            except Exception as error:
                rows.append(dict(pair_id=pair.pair_id, error=str(error)))
        emit(dict(mode="public research scan", rows=rows), args.output)
        return
    engine = Engine(args.db, pairs, settings)
    source = sys.stdin if args.events == "-" else open(args.events)
    output = open(args.output, "w") if args.output else sys.stdout
    try:
        for line in source:
            if not line.strip():
                continue
            event = json.loads(line)
            result = apply_event(engine, event)
            output.write(
                encode(
                    dict(
                        event_type=event["type"],
                        result=result,
                        pending_commands=engine.pending_commands(),
                    )
                )
                + "\n"
            )
            output.flush()
    finally:
        engine.close()
        if source is not sys.stdin:
            source.close()
        if output is not sys.stdout:
            output.close()


if __name__ == "__main__":
    main()
