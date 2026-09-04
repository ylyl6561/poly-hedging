# poly-hedging

> **Polymarket event-hedging toolkit — BTC fast markets, top-user copy trading, and dual-wallet event hedging on real USDC markets.**

[![License: BSL 1.1](https://img.shields.io/badge/License-BSL_1.1-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](requirements.txt)
[![Markets](https://img.shields.io/badge/Polymarket-BTC%20%7C%20ETH%20%7C%20SOL-blueviolet)](https://polymarket.com)

---

## What this project is

`poly-hedging` is an open-source **Polymarket event-hedging toolkit** written in
Python. It bundles three related strategies that share the same Polymarket
CLOB / data plumbing, all of which run on **real USDC markets** and ship with
a dry-run mode for paper testing.

The unifying theme is **hedging exposure** on Polymarket prediction markets —
not directional betting — so each strategy is built around explicit position
control, multi-wallet exposure management, and a publication-grade
replay / journaling layer for post-trade analysis.

### Targeted Polymarket event families

| Event family | Where it lives in Polymarket | What this project does there |
|---|---|---|
| **Crypto sprint / fast markets** (5 m, 15 m Up/Down on BTC, ETH, SOL) | Gamma API, continuous | Trades momentum via `fastloop_trader.py` with strict path-quality filters |
| **Top-wallet leaderboard trades** | Polymarket Data API (`/leaderboard`, `/activity`) | Mirrors top-N wallets into a follower account (`smart_money/`, `feature/copy_top_user`) |
| **Event-outcome markets** (politics, sports, macro, anything with YES / NO shares) | CLOB, per conditionId | Dual-wallet hedging — opens complementary legs on two wallets so exposure is bounded (`strategy/dual_wallet_*`) |

---

## ✨ What's inside

### 1. BTC 5-minute FastLoop (trend-following)

```bash
# paper trade (default)
python fastloop_trader.py

# live, quiet mode for cron
python fastloop_trader.py --live --quiet
```

- **Signal**: Binance BTC/USDT spot klines (default), with optional
  Chainlink oracle-latency confirmation via Polymarket RTDS.
- **Edge**: trend-following only — buy the side already leading at the
  window open, never inverse-mean-revert. Chop and whipsaw windows are
  rejected by a path-score filter before entry.
- **Execution**: Polymarket CLOB via the Simmer SDK (`simmer-sdk`),
  `FAK` / `FOK` in the last 30 – 3 seconds of each window.
- **Fees**: Polymarket charges ~10 % on these markets (`is_paid: true`);
  factored into every threshold.

> ⚠️ Polymarket's stop-loss / take-profit monitors check positions every
> 15 minutes, so they will never fire on 5 m or 15 m markets before
> resolution. **Do not rely on automated SL/TP for fast markets.**

### 2. Copy the top Polymarket users (跟单)

```bash
./scripts/copy_top_user.sh --top 25 --follow-pct 5
```

- Pulls the top-N wallets by recent PnL / volume.
- Mirrors their open and close trades on a follower wallet, with
  optional delay, sizing, and asset filters.
- Lets a small account replicate the strategy of proven traders without
  having to discover the alpha independently.

### 3. Dual-wallet event hedging (the namesake)

```bash
# paper-mode hedging run
python main/run_dual_wallet.py --config examples/dual_wallet_example.json

# live
python main/run_dual_wallet.py --config my-config.json --live --confirm-real-money
```

This is the strategy the project is named after. The pipeline:

1. `strategy/dual_wallet_event_strategy.py` runs an event-driven state
   machine over a `TaskManager`, so multiple events can be hedged
   concurrently.
2. Each event opens complementary positions on **two wallets** (wallet A
   buys YES on event X, wallet B buys NO on the same event). Aggregate
   exposure is bounded while the strategy still collects the spread when
   one side settles.
3. Entry is gated on:
   - Aggregate-imbalance on the CLOB order book (`market/`).
   - A fair-value estimate vs. oracle / external reference price.
   - A slippage / fee-aware minimum edge per leg.
4. Both legs are post-only GTC orders when possible, with FOK fallback
   for thin books. Mismatched fills trigger a rebalance via FAK to
   flatten residual exposure.
5. Settlement / unwind uses the relayer-based redeem flow on Polygon.

What "hedging" means here:

- **Internal hedging** across two wallets, so a single bad outcome
  cannot drain one wallet's USDC.
- **External hedging** — when holding a directional Polymarket position
  (from either fast-loop or copy-trade), the dual-wallet framework lets
  you lay off tail risk on a correlated event before settlement.

---

## 📦 Module layout

| Module | Role |
|---|---|
| `fastloop_trader.py` | Strategy 1 entry point — BTC fast-loop |
| `strategy/dual_wallet_event_strategy.py` | Strategy 3 entry point — event strategy |
| `strategy/dual_wallet_executor.py` | CLOB / relayer execution adapter |
| `strategy/dual_wallet_models.py` | Event + leg + result dataclasses |
| `strategy/account_pool.py` | Multi-wallet registry / rotation |
| `accounts/` | Wallet contexts, signing keys, per-account state |
| `market/` | Polymarket order book + microstructure utilities |
| `core/` | Config resolution + shared helpers |
| `state/` | Structured run logs + candidate journals |
| `scheduler/`, `main/` | Loop driver + cron-free heartbeat scheduling |
| `notifications/` | Feishu / Telegram / stdout notifiers |
| `smart_money/` | Top-user scraping + copy-trade fan-out |
| `trading/`, `api/` | CLOB SDK wrapper, Gamma + Data API client |
| `runs/`, `logs/` | Per-cycle run output and trade journals |
| `scripts/` | Replay, backfill, ops scripts |
| `tests/` | Replay tests + dry-run regressions |

---

## 🚀 Quick Start

```bash
git clone https://github.com/ylyl6561/poly-hedging.git
cd poly-hedging

python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# try the BTC fast-loop in dry-run mode (no real money)
python fastloop_trader.py

# 15-minute paper observation
./scripts/run_fastloop_path_score.sh 900

# replay a candidate journal offline
python replay_candidate_journal.py path/to/candidates.jsonl
```

The runner writes logs and replay output under `runs/`.

---

## 🔐 Safety

> This software is provided for educational and research purposes only.
> It is not financial advice. Trading on Polymarket involves substantial
> risk of loss.

- **Dry-run is the default** for every entry point. `--live` (and on
  dual-wallet, an additional `--confirm-real-money` flag) is required
  for real orders.
- Local secrets live in `.env` only — gitignored, env vars take priority.
- For Polymarket direct-CLOB live trading, the wallet private key signs
  client-side; no key is uploaded to a third party.
- Polymarket markets can move on news, oracle revisions, and resolution
  disputes. Past dry-run performance does not guarantee live results.

The maintainers make **no claims of profitability**, are **not registered**
as investment advisers or broker-dealers in any jurisdiction, and accept
**no liability** for trading losses incurred using this software. You are
solely responsible for compliance with local laws — the US, UK, and
several other jurisdictions restrict retail access to prediction markets.

---

## 🔧 Configuration

Local config defaults are in `config.example.json`. Copy it first:

```bash
cp config.example.json config.json
```

Useful overrides from the CLI:

```bash
python fastloop_trader.py --set asset=BTC
python fastloop_trader.py --set strategy_mode=oracle_latency
python fastloop_trader.py --set execution_route=direct_clob
python fastloop_trader.py --set order_type=FAK
```

`execution_route` can be `direct_clob` (default, your own wallet signs)
or `simmer_wallet` (routes through the Simmer SDK wallet trade path).

Environment variables:

```bash
SIMMER_API_KEY="..."           # required, from simmer.markets/dashboard
WALLET_PRIVATE_KEY="..."       # only for direct_clob live trading
TRADING_VENUE="polymarket"     # default
```

---

## 🧪 Tests and replay

```bash
pytest tests/                      # 45+ tests
python fastloop_trader.py          # paper
python replay_candidate_journal.py # offline replay
./scripts/run_fastloop_path_score.sh 900
```

---

## 💼 Get the Toolkit

The open-source core above is self-contained and free to use. For the **paid
toolkit** (production config presets, hedging calculator UI, PnL-attribution
replay, 1-hour onboarding call, 6-week exclusive updates, white-label rights,
12 months Discord support) — one tier, one price:

### Polymarket Trader Toolkit — **$99** (one-time)

Includes:
- All open-source code (BSL 1.1)
- 3 production config templates (BTC 5m FastLoop, smart-money copy trader, dual-wallet hedge)
- Hedging calculator UI
- PnL-attribution replay
- Feishu / Discord / Telegram notifier templates
- 1-hour onboarding call (Zoom)
- 6 weeks of exclusive strategy updates
- 12 months Discord support
- White-label rights (apply)
- Lifetime updates

**→ [Buy on Gumroad — $99](https://liangyu5.gumroad.com/l/polymarket-toolkit)**

Use code `FIRST48` for 20% off in the first 48 hours. USDC on Polygon also
accepted — see [`commercial/pricing/payment.md`](commercial/pricing/payment.md).

---

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug fixes are welcome as PRs.
New strategies belong in the paid toolkit.

---

## 📜 License

**Business Source License 1.1** — see [LICENSE](LICENSE).

You may use, modify, and self-host this software for personal or
internal business use. You may **not** resell or sublicense it. After
4 years, each release automatically converts to Apache 2.0.

For commercial redistribution rights, see the **$99 toolkit** tier.

---

## 🌟 Acknowledgments

Built by traders who lost money first, then wrote the tools they
wished they'd had. Inspired by `ccxt`, `hummingbot`, and `freqtrade`
— but for prediction markets.
