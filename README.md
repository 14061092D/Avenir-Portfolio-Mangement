# Avenir Portfolio Management

The [carry strategy](carry_strategy/README.md) implements spot–perpetual pair discovery, cost-adjusted entry signals, 500 USDT maker slices, fill-driven IOC hedge intents, durable reconciliation and long-hold management.

Run the deterministic demonstration from the repository root.

```sh
python3 -m carry_strategy demo --output-dir /tmp/avenir-carry-demo
python3 -m unittest discover -s carry_strategy/tests -v
```

Public market-data adapters cover Binance, Bitget and OKX. Connecting authenticated broker execution and account-specific corporate-action handling remains integration work.

## Hyperliquid BTC spot/perpetual pilot

The separate [BTC spot/perpetual pilot](btc_spot_perp/README.md) uses a 500 USDC total budget, approximately 20 USDC maker entries at a 100 bp offset, fill-driven perpetual hedges, and fee-aware maker exits. It defaults to public-data shadow execution. The live adapter requires explicit operator activation and has not been validated with real orders.

Run this package from its own directory:

```sh
cd btc_spot_perp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
avenir-mm shadow --seconds 300 --config configs/btc_500.yaml
```

The carry strategy and BTC pilot have separate configuration and execution state. The BTC package was imported from local commit `8403dd39ec68e1ad858f035dc1ef6662c31e9be4`; its initial verification record is retained in `btc_spot_perp/docs/verification_20261109.json`.
