# Avenir BTC spot–perpetual pilot

This pilot starts with USDC, posts one BTC spot bid below the executable BTC perpetual price, and hedges actual spot fills. It then posts a fee-aware spot exit and covers the corresponding short hedge as the spot sells.

The default mode uses real public Hyperliquid market data and simulated orders. Live execution is implemented as a separate adapter but has not been validated with real orders. This repository does not establish profitability or production readiness.

## Run the public-data simulation

Use Python 3.11 or later on macOS or Linux. Run these commands from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
avenir-mm shadow --seconds 300 --config configs/btc_500.yaml
```

No wallet or signing key is needed for `shadow`. The model starts with 500 simulated USDC, divided equally between spot cash and perpetual collateral. The default entry size is approximately 20 USDC after quantity rounding. The inventory ceiling is 100 USDC.

The command prints `started`, periodic `status`, any `action`, and a final `finished` event. Read `halted_reason`, `pending`, and the two BTC quantities in the status. An `error` event means the run did not complete normally.

Inspect the saved state after the run:

```sh
avenir-mm status --state-dir runtime/shadow
```

A five-minute run may produce no fills. A resting bid 100 bp below the hedge price is not expected to trade continuously. Zero simulated fills in a short run does not measure the strategy's daily opportunity rate.

## Exercise the deterministic demonstration

```sh
avenir-mm demo
```

The demonstration uses constructed events to exercise the trading cycle. Its outcome is a software check, not a market result.

## Interpret shadow fills

The paper adapter requires an observed spot trade to pass strictly through the resting price, after a modeled 200 ms order-arrival delay. It allocates at most 10% of that public print's quantity to the simulated order. Perpetual IOC fills use the visible book and the configured price limit.

These assumptions do not reproduce queue position, matching priority, market impact, or live order latency. The paper adapter charges configured trading fees but does not accrue funding payments. Simulated profit is therefore not evidence of achievable net profit. See [the execution and accounting design](docs/architecture.md).

## Prepare an explicitly selected live run

The current live adapter supports the mainnet UBTC spot market and BTC perpetual only. It requires a dedicated ordinary trading account with account abstraction explicitly set to `disabled`. Unknown, `default`, unified, portfolio-margin, vault, and subaccount modes are rejected.

Live startup takes an account-and-network lock shared across state directories for the same OS user. It cannot detect a bot on another machine or OS user. Do not operate another bot or manually trade the dedicated account while this process is running.

For a fresh strategy, the account must contain no BTC position or open orders. Fund spot cash and perpetual collateral manually within the configured pilot capital. The adapter requires enough spot cash for the entry and at least 50 USDC of withdrawable perpetual collateral. It does not buy initial BTC, transfer funds, change account mode, or configure leverage.

1. Copy `configs/btc_500.yaml` to `configs/btc_500_live.yaml`.
2. Change `mode` to `live` in the copy.
3. Set `HL_ACCOUNT_ADDRESS` to the dedicated trading account address.
4. Set `HL_AGENT_PRIVATE_KEY` locally to an API agent key approved for that account. Do not use a seed phrase or a master-wallet private key.
5. Keep the key out of YAML files, saved state, logs, and Git.
6. Start the explicit live command only after reviewing the account and configuration:

```sh
avenir-mm live --seconds 300 --config configs/btc_500_live.yaml --confirm-live
```

Both the live configuration and `--confirm-live` are required. The adapter verifies that the signing address is an approved agent for `HL_ACCOUNT_ADDRESS`. The repository does not ship an enabled live configuration.

Live sessions are limited to 1,800 seconds per supervised run. A duration limit stops the session, not necessarily its remaining position.

## Inspect a stopped or interrupted run

Read the saved status before restarting. Reuse the same state directory for the same strategy instance. Do not delete its database to bypass an unresolved order or a reconciliation halt.

After a normal `session_complete` or `operator_stop`, resume the same shadow instance with:

```sh
avenir-mm shadow --seconds 300 --config configs/btc_500.yaml --resume-clean-stop
```

The flag does not clear other halt reasons. A changed configuration or a different live account cannot reuse the existing state directory.

The strategy stops new entries and cancels owned maker orders when a safety condition is reached. Existing hedges can remain open. A stop, process exit, or exchange dead-man cancellation does not guarantee flat positions or a maximum loss. Check both spot inventory and the perpetual position before considering the run closed.

The default controls include a 5 USDC drawdown from the highest observed equity in the preceding 24 hours, a 10 USDC drawdown from the run's equity high-water mark, and a one-hour holding limit. These controls trigger a halt. They are not guaranteed execution prices or automatic liquidation instructions.

Partial fills can leave less than the 10 USDC minimum order amount. The strategy retains the residual in its ledger. It can briefly retain the current maker remainder to let small fills accumulate, then halt if the residual remains unhedgeable. Inspect the exact residual and halt reason rather than assuming that every fill was fully hedged.

## Run the local checks

```sh
python -m pytest -q
```

Local checks cover strategy decisions and adapter behavior with controlled inputs. They do not certify live exchange behavior. The package pins `hyperliquid-python-sdk==0.24.0`.

Read [the architecture](docs/architecture.md) for state ownership and recovery. Read [the rate-limit reference](docs/rate_limits.md) before changing quote frequency or account polling.
