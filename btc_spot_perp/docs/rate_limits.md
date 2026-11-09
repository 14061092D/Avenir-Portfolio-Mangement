# Hyperliquid rate limits used by this pilot

Reference date: 9 November 2026. Exchange limits can change. The values below come from [Hyperliquid's official rate-limit documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).

## Exchange quotas

| Scope | Published quota or weight |
| --- | --- |
| REST per IP | 1,200 weighted units per minute |
| Exchange request | `1 + floor(batch_length / 40)` weight |
| Common book, balance, and order-status reads | 2 weight |
| `userRole` | 60 weight |
| Other documented info reads | 20 weight, plus response-size charges where specified |
| WebSocket connections | 10 concurrent; 30 new connections per minute |
| WebSocket subscriptions | 1,000; at most 10 distinct subscribed users |
| Outgoing WebSocket messages | 2,000 per minute across connections |
| In-flight WebSocket posts | 100 |

REST's quota is weighted. It is not a blanket allowance of 20 requests per second. Batched actions can reduce IP cost while each constituent order or cancellation still consumes an address request.

An address starts with 10,000 action requests and earns one request per cumulative USDC traded. The cancellation ceiling is `min(limit + 100000, limit × 2)`. Address-limited accounts receive one request every ten seconds. Info reads do not consume this action allowance. These are distinct from IP limits.

## Local limits

`LiveVenue` maintains a rolling 60-second budget of 900 REST weight units for its guarded calls. Two hundred units are reserved for critical operations, leaving 700 for ordinary calls. This local budget does not account for unrelated programs sharing the IP.

The adapter polls normal account state at most once every 15 seconds, with forced refreshes around events that require reconciliation. It refreshes the reported address quota and funding history at most once per minute during ordinary operation. Response-size charges are added for returned fill and funding records.

Before a new spot entry, the adapter requires more than 1,000 address requests remaining. Cancellation and hedge actions can use this local reserve. The reserve does not guarantee that the exchange accepts an action, and the adapter does not treat the exchange's larger cancellation allowance as unlimited local capacity.

`RateBudgetExceeded` stops blind retries when the local or verified address budget is unavailable. Unknown submission outcomes are resolved by order identity instead of resubmitting a new order.

## Quote policy

The defaults limit ordinary entry repricing through `quote_min_life_ms=60000` and `reprice_bp=5`. An unchanged rounded target price creates no replacement order. A price change alone is not permission to cancel every market-data update.

Safety cancellation bypasses the normal quote lifetime. For example, an existing entry with less than `safety_cancel_edge_bp=80` remaining edge can be canceled before 60 seconds. Exit orders can also be canceled sooner when their size or required profit price changes.

Cancellation and replacement are separate commands. The runner resolves each command before sending another. The pilot does not batch exchange actions.

Low fill frequency makes the address quota important. Quotes, cancellations, and dead-man refreshes consume requests even when a 100 bp spot bid never fills. Public WebSocket updates do not replenish the address allowance.

## Dead-man and expiry overhead

The adapter refreshes its scheduled cancel every 30 seconds. Each refresh is an exchange action. A five-second `expiresAfter` deadline also applies to signed actions. Hyperliquid charges five times the normal address cost when an action expires stale. See [exchange action expiry](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint).

The local action counter is an operating estimate, not a complete exchange billing ledger. Periodic `userRateLimit` reads reconcile the reported remaining allowance. Actual limits remain shared with other activity on the same account or IP.
