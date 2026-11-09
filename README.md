# Avenir Portfolio Management

The [carry strategy](carry_strategy/README.md) implements spot–perpetual pair discovery, cost-adjusted entry signals, 500 USDT maker slices, fill-driven IOC hedge intents, durable reconciliation and long-hold management.

Run the deterministic demonstration from the repository root.

```sh
python3 -m carry_strategy demo --output-dir /tmp/avenir-carry-demo
python3 -m unittest discover -s carry_strategy/tests -v
```

Public market-data adapters cover Binance, Bitget and OKX. Connecting authenticated broker execution and account-specific corporate-action handling remains integration work.
