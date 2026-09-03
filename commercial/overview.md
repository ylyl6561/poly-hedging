# poly-hedging — Project Overview

## What this project is

`poly-hedging` is an open-source **Polymarket event-hedging toolkit** written in
Python. It bundles three related strategies that share the same Polymarket
CLOB / data plumbing, all of which run on **real USDC markets** and ship with
a dry-run mode for paper testing.

The unifying theme is **hedging exposure** on Polymarket prediction markets —
not directional betting — so each strategy is built around explicit position
control, multi-wallet exposure management, and a publication-grade
replay/journalling layer for post-trade analysis.

---

## Strategy 1 — BTC 5-minute / 15-minute FastLoop (trend-following)

Target Polymarket event family: **Polymarket's crypto sprint / fast markets**
(`will BTC go up in the next 5 minutes`, etc.). These are the high-frequency
Up/Down markets that Polymarket runs continuously on BTC, ETH, and SOL.

- **Signal source**: Binance BTC/USDT spot klines (default), with optional
  Chainlink oracle-latency confirmation via Polymarket RTDS.
- **Edge**: trend-following only — buy the side already leading at the window
  open, never inverse-mean-revert. Chop and whipsaw windows are rejected by
  a path-score filter before entry.
- **Execution**: Polymarket CLOB via the Simmer SDK (`simmer-sdk`), direct
  CLOB signing with the local wallet, `FAK` / `FOK` order types in the
  last 30–3 seconds of each window.
- **Fees**: Polymarket charges ~10% on these markets (`is_paid: true`) —
  factored into every threshold.

Entry point: `fastloop_trader.py`. The shipped `run_fastloop_path_score.sh`
runs an indefinite paper-trade loop under `runs/`.

> Risk note: Polymarket's stop-loss / take-profit monitors check positions
> every 15 minutes, so they will never fire on 5m or 15m markets. Size
> accordingly and **don't rely on automated SL/TP for fast markets.**

---

## Strategy 2 — Copy-trading the top Polymarket users (跟单)

Target Polymarket data: **public wallet leaderboard and trade history**
served via Polymarket's Data API (positions, activity, leaderboard).

- Pulls the top-N wallets by recent PnL / volume.
- Mirrors their open and close trades on a follower wallet, with optional
  delay, sizing, and asset filters.
- Lets a small account replicate the strategy of proven traders without
  having to discover the alpha independently.

Relevant modules: `smart_money/`, `feature/copy_top_user` branch,
`scripts/data_scraping/` style utilities.

---

## Strategy 3 — Dual-wallet event hedging (核心对冲)

Target Polymarket events: **event-outcome markets** (politics, sports, macro
binary markets, etc.) — not the 5-minute fast markets.

This is the strategy the project is named after. The pipeline:

1. `strategy/dual_wallet_event_strategy.py` runs an event-driven state
   machine over a `TaskManager` so multiple events can be hedged
   concurrently.
2. Each event opens complementary positions on two wallets (e.g. wallet A
   buys YES on event X, wallet B buys NO on the same event) so the
   aggregate exposure to mispricing is bounded while still collecting
   the spread when one side settles.
3. Entry is gated on:
   - Aggregate-imbalance on the CLOB order book (`market/`).
   - A fair-value estimate vs. oracle / external reference price.
   - A slippage / fee-aware minimum edge per leg.
4. Both legs are post-only GTC orders when possible, with FOK fallback for
   thin books. Mismatched fills trigger a rebalance via FAK to flatten
   residual exposure.
5. Settlement / unwind uses the relayer-based redeem flow on Polygon.

Core modules:

| Module | Role |
|---|---|
| `strategy/dual_wallet_event_strategy.py` | Event strategy entry point |
| `strategy/dual_wallet_executor.py` | Execution adapter (CLOB / relayer) |
| `strategy/dual_wallet_models.py` | Event + leg + result dataclasses |
| `strategy/account_pool.py` | Multi-wallet registry / rotation |
| `accounts/` | Wallet context, signing keys, per-account state |
| `market/` | Polymarket order book + microstructure utilities |
| `core/` | Config resolution, shared helpers |
| `state/` | Structured run logs, candidate journals |
| `scheduler/`, `main/` | Loop driver / cron-free heartbeat scheduling |
| `notifications/` | Feishu / Telegram / stdout notifiers |
| `smart_money/` | Top-user scraping + copy-trade fan-out |
| `trading/`, `api/` | CLOB SDK wrapper, Gamma + Data API client |
| `runs/`, `logs/` | Per-cycle run output and trade journals |
| `tests/` | Replay tests + dry-run regressions |

---

## What "hedging" means here

The project uses the word in two senses:

1. **Internal hedging** — across two wallets on the same event, so a
   single bad outcome cannot drain one wallet's USDC. Aggregate exposure
   on the platform stays roughly market-neutral.
2. **External hedging** — when holding a directional Polymarket position
   (from either fast-loop or copy-trade), the dual-wallet framework lets
   you lay off tail risk on a correlated event before settlement.

---

## Repo layout (top-level)

```
poly-hedging/
├── README.md                     (project root readme)
├── SKILL.md                      (OpenClaw / Codex skill metadata)
├── clawhub.json                  (Simmer SDK skill manifest)
├── fastloop_trader.py            (Strategy 1 entry point)
├── config.example.json           (Conservative BTC 5m defaults)
├── requirements.txt
│
├── commercial/                   (Launch kit: landing, pricing, growth)
│   ├── plan.md
│   ├── overview.md               (← this file)
│   ├── landing/index.html
│   ├── github-repo/README.md     (Public-facing project description)
│   ├── github-repo/LICENSE       (BSL 1.1)
│   ├── github-repo/CONTRIBUTING.md
│   ├── pricing/payment.md
│   ├── marketing/x-reddit/content.md
│   └── growth/100-users.md
│
├── accounts/                     (Wallet contexts + signing)
├── strategy/                     (Three strategies + task manager)
├── market/                       (Polymarket order book / microstructure)
├── trading/                      (CLOB SDK + execution wrappers)
├── api/                          (Polymarket Gamma + Data API client)
├── core/                         (Config, utilities)
├── state/                        (Run logs, structured journals)
├── scheduler/ main/              (Loop driver + cron-free scheduling)
├── smart_money/                  (Top-user scrape + copy trading)
├── notifications/                (Feishu / Telegram / stdout)
├── runs/ logs/                   (Per-cycle output, trade journals)
├── scripts/                      (Replay, backfill, ops scripts)
└── tests/                        (Replay tests + dry-run regressions)
```

---

## Safety defaults

- **Dry-run is the default** for `fastloop_trader.py`. `--live` is required
  for real orders.
- Local secrets live in `.env` only — gitignored, env vars take priority
  over file values.
- For Polymarket direct-CLOB live trading, the wallet private key signs
  client-side; no key is uploaded to a third party.
- Strategy 3 (dual-wallet event hedging) is **paper-only by default**.
  A real-money switch must be flipped twice (config + explicit CLI flag)
  before any leg executes.

This repository is strategy / research tooling, not financial advice.
Past dry-run performance does not guarantee live results. Polymarket
markets can move on news, oracle revisions, and resolution disputes.
