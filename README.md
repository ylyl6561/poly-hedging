# poly-simmer-fast-loop

Polymarket BTC fast-market trading research project using the Simmer SDK.

The strategy trades BTC 5-minute or 15-minute Up/Down markets from CEX momentum and a fair-value model, with additional filters for trend confirmation, chop, and full-window path quality.

## What is included

- `fastloop_trader.py` - one-cycle trader, dry-run by default, `--live` for real orders.
- `replay_candidate_journal.py` - offline replay tool for candidate journals.
- `scripts/run_fastloop_path_score.sh` - loop runner with the current path-score configuration.
- `config.example.json` - example conservative BTC 5m configuration.
- `clawhub.json` / `SKILL.md` - OpenClaw / Codex skill metadata and instructions.

## Safety

Do not commit real secrets. Put local secrets in `.env` or use environment variables:

```bash
SIMMER_API_KEY="..."
WALLET_PRIVATE_KEY="..."  # only needed for live self-custody trading
TRADING_VENUE="polymarket"
```

`fastloop_trader.py` automatically reads `.env` from this directory before loading
configuration. Existing shell environment variables take priority over `.env`.
The `.env` file is ignored by git.

For live Polymarket trades, the script defaults to direct CLOB execution with
`execution_route=direct_clob`, using `WALLET_PRIVATE_KEY` to sign locally.
Set `execution_route=simmer_wallet` to route live orders through Simmer SDK
wallet trade execution and its wallet auto-link flow instead. The legacy
`SIMMER_FASTLOOP_DIRECT_CLOB=false` environment variable still selects the
Simmer wallet route when `execution_route` is not set.

Dry-run is the default. Real orders require explicit `--live`.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run one dry-run cycle

```bash
python3 fastloop_trader.py
```

## Run a 15-minute paper observation

```bash
./scripts/run_fastloop_path_score.sh 900
```

The runner writes logs and replay output under `runs/`.

## Run live

Only run live after confirming wallet, balance, and budget:

```bash
python3 fastloop_trader.py --live
```

## Current strategy notes

The current recommended mode is trend-following only:

- Buy the side already leading relative to the market window open.
- Avoid inverse mean reversion.
- Require recent momentum to agree with the leading side.
- Use macro momentum as confirmation.
- Reject near-open chop and whipsaw paths.
- Score the observed window path before accepting a trade.

This repository is strategy/research tooling, not financial advice.

## Oracle-latency observation mode

The experimental oracle-latency mode monitors Polymarket RTDS prices for both
Binance (`crypto_prices`) and Chainlink (`crypto_prices_chainlink`). It is meant
to test whether Binance moves ahead of the Chainlink settlement stream near the
end of a fast market.

Run it in dry-run mode first:

```bash
python3 fastloop_trader.py --oracle-latency
python3 fastloop_trader.py --oracle-latency --loop --loop-interval 10
python3 fastloop_trader.py --oracle-latency --scheduled-loop --loop-interval 5
```

Or persist the mode in config:

```bash
python3 fastloop_trader.py --set strategy_mode=oracle_latency
python3 fastloop_trader.py --set execution_route=direct_clob
python3 fastloop_trader.py --set order_type=FAK
```

Important operating notes:

- The bot must have a Chainlink sample near the window open to recover the
  price-to-beat. The local loop records RTDS samples and can recover the open
  price later in the same market.
- `--scheduled-loop` is preferred for oracle-latency: it sleeps until each
  fixed 5m slot opens, samples once at open + 2s, then sleeps until the final
  entry window and evaluates every `--loop-interval` seconds.
- The entry window defaults to the last 30 to 3 seconds.
- The Chainlink settlement NO add-on can enter only when Chainlink is already
  clearly below the window open near settlement, the NO ask is below its cap,
  and Binance has not crossed strongly back above the open.
- Live use should prefer direct CLOB execution and `FAK` or `FOK` orders.
- If you want Simmer wallet execution instead, set
  `execution_route=simmer_wallet`; that keeps the existing Simmer import,
  wallet auto-link retry, and `client.trade(...)` path.
- Keep `--live` off until candidate logs show a real, repeatable edge after
  fees, spread, stale ticks, and missed fills.
