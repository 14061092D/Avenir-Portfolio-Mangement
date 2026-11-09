# Carry strategy

A Python strategy for spot purchases with a matched short linear perpetual. It discovers eligible pairs, ranks cost-adjusted carry, and emits persistent maker and hedge order intents.

The package includes public market-data clients for Binance, Bitget, and OKX. Authenticated broker transport remains an integration task. The engine supplies the order state, dispatch protocol, and reconciliation contract that a broker adapter consumes.

Python 3.10 or later and the Python standard library are required. Commands run from the directory that contains the `carry_strategy` package.

## Commands

The deterministic demo exercises order submission, spot fills, hedge fills, and persisted state. Its output directory must have no existing `state.sqlite` file.

```sh
python -m carry_strategy demo --output-dir /tmp/carry-demo
```

Discovery writes normalized pairs with explicit fee assumptions.

```sh
python -m carry_strategy discover \
	--venue bitget \
	--maker-fee-bps 1 \
	--hedge-fee-bps 1 \
	--output pairs.json
```

A scan reads public market data and reports entry decisions for the configured pairs.

```sh
python -m carry_strategy scan --config carry_strategy/example_config.json
```

Replay feeds a normalized event log into a persistent engine database. The generated demo log can be replayed into a new database.

```sh
python -m carry_strategy replay \
	--config /tmp/carry-demo/config.json \
	--db /tmp/carry-replay.sqlite \
	--events /tmp/carry-demo/events.jsonl
```

The unit tests run through the public module interfaces and the durable engine.

```sh
python -m unittest discover -s carry_strategy/tests -v
```

## Entry and holding rules

The first entry in a campaign requires a P95 deviation signal and a positive cost-adjusted holding-period estimate. The default fair basis uses 24 closed hourly observations. The percentile uses 720 prior deviations.

The daily funding forecast uses the lower of the seven-day and thirty-day historical means. Negative special payments reduce that forecast. Positive one-off credits do not increase it. Data coverage and freshness are checked before the estimate is used.

The entry estimate combines the proposed maker price, quantity-specific hedge VWAP, forecast funding, and estimated exit basis. It subtracts entry and exit fees plus an execution reserve. Accepted candidates compete for the configured capital limit according to expected return on reserved capital.

Each maker order targets at most 500 USDT by default. A partial maker fill creates a hedge requirement from the actual net base quantity. Existing inventory dust joins the next hedge calculation. Each order uses the venue's quantity step.

After the first fill, the campaign continues to add eligible slices while its cost and capital conditions remain satisfied. The P95 condition controls campaign entry. Every resting entry order is still re-evaluated against fresh market data.

The default holding policy has no fixed expiry. An operator exit, a configured holding limit, or a negative carry forecast that reaches the confirmation timer starts a staged unwind. The default negative-carry confirmation period is 24 hours. Missing funding estimates reset that timer. Spot sell fills trigger reduce-only perpetual buy orders. An operator exit pauses subsequent entry until a `resume` event.

## Configuration

[example_config.json](example_config.json) contains one Binance PAXG pair. Its one-basis-point maker and hedge fees are examples. They are not verified account rates. The instrument steps reflect the public metadata checked on November 9, 2026.

Decimal values use JSON strings to preserve exact quantities. `pairs` contains serialized `Pair` objects with a spot `maker` and a linear perpetual `hedge`. `engine` contains `EngineConfig`, including its nested `signal` configuration.

| Field | Meaning |
| --- | --- |
| `pair_id` | Stable strategy identity retained across restarts |
| `underlying` | Explicit common asset identity on both legs |
| `contract_size` | Underlying base units per venue order unit |
| `qty_step` | Venue order-quantity increment |
| `maker_fee_bps`, `hedge_fee_bps` | Configured fees used by the signal calculation |
| `fee_in_base` | Whether a maker purchase is expected to deduct its fee from received base inventory |
| `mapping_verified` | Whether the spot and perpetual identity has been checked |
| `corporate_actions_ready` | Whether the integration handles the pair's corporate-action quantities and cash flows |
| `target_notional` | Maximum spot cost assigned to the pair |
| `hedge_margin_fraction` | Reserved quote cash per unit of hedge notional |
| `maker_route` | Venue routing option carried into maker commands |
| `capital_limit` | Shared strategy capital limit, including reserved hedge cash and pending orders |
| `max_active_pairs` | Maximum number of simultaneous pair campaigns |
| `max_unhedged_notional` | Residual exposure limit in USDT |
| `max_hold_days` | Optional campaign holding limit measured from its first fill |
| `signal.horizon_days` | Funding-estimate horizon, independent of the holding limit |
| `min_capital_apr` | Minimum annualized expected profit divided by reserved capital, expressed as a decimal fraction |

Public discovery automatically pairs exact BTC, ETH, PAXG, and XAUT identities where both required products exist. Stocks need an explicit mapping. Their on-platform quantities must already represent effective shares.

An approved Bitget stock-token mapping uses `maker_route="internal_market"`. Its corporate-action readiness remains separate from symbol matching. Raw token multipliers require conversion before input enters this engine. Share changes and dividend cash flows also require a corporate-action accounting integration. The package does not supply that workflow.

Natural gas remains a separate research candidate. The core pair model requires a spot leg. NATGAS and NG cross-exchange perpetual pairs also need index and contract-roll alignment, which this spot-perpetual package does not implement.

## Quantity and time conventions

All `Book` quantities, position quantities, execution quantities, and `qty_base` fields use normalized underlying base units. Order command `qty` uses the exchange's native order units.

For example, an OKX contract with `contract_size="0.01"` represents 0.01 BTC. An intent with `qty_base="0.02"` therefore has `qty="2"` contracts. The adapter applies the same conversion to execution quantities and order-status cumulative fills.

Prices are USDT per underlying base unit. Timestamps are Unix milliseconds. Closed-hour basis samples use the bar-close timestamp. Book timestamps include request latency so a slow response can become stale.

Execution fees specify `fee_currency="base"` or `fee_currency="quote"`. `fee_amount` uses the corresponding currency units. A base fee on a spot purchase reduces the inventory available for hedging. An unsupported fee currency pauses new entries until the accounting is resolved.

## Replay event format

Each JSONL line contains one object with a `type` field. Decimal quantities and prices use strings. The demo writes a complete event log to `events.jsonl`.

| Event type | Required fields |
| --- | --- |
| `market` | `market` containing a serialized `Market`, `now_ms` |
| `tick` | `now_ms` |
| `fill` | `venue`, `execution_id`, `client_order_id`, `qty_base`, `price`, `fee_amount`, `fee_currency`, `time_ms` |
| `order` | `client_order_id`, `status`, `cumulative_qty_base`, `now_ms` |
| `dispatch` | `command_id`, `now_ms` |
| `reconcile` | `positions`, `orders`, `available_quote`, `now_ms` |
| `exit` | `pair_id`, `now_ms`, optional `reason` |
| `resume` | `pair_id` |

A `Market` contains `pair_id`, `maker_book`, `hedge_book`, `basis_history`, and `funding`. Each book contains `bids`, `asks`, and `received_ms`. Each level is a two-item price and base-quantity array.

A basis observation contains `timestamp_ms` and `basis_bps`. A funding observation contains `timestamp_ms`, `rate`, `interval_ms`, and `kind`. Supported kinds are `regular` and `special`.

A reconciliation `positions` object maps each configured pair ID to `spot` and `short` base quantities. Its `orders` object maps unresolved strategy client IDs to `status` and `cumulative_qty_base`. `available_quote` maps each venue to its available USDT cash.

Replay emits one result per event, including the current queued commands. A repeated command in that output still has the same command ID. The dispatch protocol determines whether it can be sent.

## Broker adapter contract

The engine emits `place`, `cancel`, and `reconcile` commands through its SQLite outbox. Place commands contain the stable client order ID, venue, symbol, side, price, native `qty`, normalized `qty_base`, time in force, and route.

Maker commands use `PostOnly`. Hedge commands use a bounded-price `IOC`. A hedge buy that closes an existing short also sets `reduce_only=true`. The adapter translates these names to the venue's API values.

The dispatcher calls `mark_dispatched(command_id, now_ms)` before any network send. A false return means the command is no longer dispatchable. A timed-out place request keeps its client order ID and reservation until the adapter resolves that ID with the exchange.

Execution callbacks call `on_fill` with the venue execution ID and the original client order ID. Duplicate execution IDs are idempotent when their payloads agree. Conflicting duplicates raise an error. Order acknowledgements and cumulative status reports alone do not change inventory.

`reconcile` receives a coherent dedicated-account snapshot containing each pair's spot and short base quantities, unresolved strategy orders, and available quote cash by venue. Missing order outcomes retain reservations and block new entry. Reconciliation includes executions that arrived before the snapshot.

Every restart requests reconciliation and cancels known resting maker orders. The database contains the execution deduplication keys, order records, pending commands, inventory, and capital reservations. One engine process and thread own a database and its broker dispatch loop. Broker callbacks are serialized onto that thread.

## Validation scope

The tests cover duplicate fills, partial hedges, late fills after cancellation, unknown order outcomes, cumulative quantity gaps, restart reconciliation, shared capital, and quantity conversion. Public-data tests cover incomplete history, stale metadata, error responses, and exact-asset pairing.

The public clients were also exercised against real public endpoints. Those checks validate data parsing and coverage. They do not measure maker queue position or prove a live execution APR.

The package's hedge-margin fraction is a cash reservation model. Exchange maintenance tiers, intrahour liquidation, ADL, transfers, and account-specific collateral rules belong to the broker and account integration.
