# Smart Money Tracker (Phase 4 · Trading console)

Read-only pipeline + dashboard that surfaces "smart money" on Polymarket so you can
decide what to copy before opening an order.  **Phase 4** adds a dedicated Trading tab
with a manual order entry form (with mandatory 二次确认), a leader-already-sold
detector (publishes OrderEvents + Feishu alert), and moves the SSE live-event
stream into the same tab.  Real (live_trade=1) execution is supported but disabled
by default — every submission is **DRY-RUN** unless `SMART_MONEY_LIVE_TRADE=1`.

> **Phase 5 暂缓** — chain reconciliation (B-chain verification) is held back until
> the operator has validated the minimum-order path end-to-end via this dashboard.

## What it does

1. Pulls the public Trader Leaderboard (`/v1/leaderboard`).
2. Walks each tracked wallet's `/activity`, `/positions`, `/closed-positions`.
3. Enriches every condition id via Gamma `/markets`.
4. Stores everything in a local PostgreSQL database.
5. Exposes a FastAPI dashboard answering the six initial questions:
   - Q1: top profitable accounts in the last 90 days
   - Q2: markets they primarily trade
   - Q3: average lead time before placing bets
   - Q4: price distribution of their bets
   - Q5: which top traders are currently placing bets
   - Q6: are multiple top traders agreeing on the same direction?

## Layout

```
smart_money/
  config.py           - env-driven settings (database DSN, intervals, limits)
  db.py               - SQLAlchemy engine + session factory
  models.py           - smart_money_* tables
  client.py           - httpx-based read client for Data API + Gamma
  normalization.py    - parsing + hashing helpers
  collector.py        - pipeline that fills the tables
  analytics.py        - the six analytical queries
  dashboard_app.py    - FastAPI app (GET /api/..., POST /api/collect/...)
  dashboard/
    index.html        - vanilla JS + Chart.js dashboard
  cli.py              - python -m smart_money ...
scheduler/
  index.js            - BullMQ + Pino scheduling, calls into the Python CLI
```

## Setup

```bash
# 1) PostgreSQL (any version 13+)
createdb polymarket_smart_money

# 2) Python deps
. .venv/bin/activate
pip install -r requirements.txt

# 3) DB schema
export SMART_MONEY_DATABASE_URL='postgresql+psycopg://yuliang:123456@localhost:5432/polymarket'
python -m smart_money init-db

# 4) Run a full collection cycle (read-only)
python -m smart_money run --job all

# 5) Start the dashboard
python -m smart_money serve --host 0.0.0.0 --port 8088
# open http://localhost:8088/
```

### Optional: Node scheduler (BullMQ + Redis)

```bash
cd scheduler
npm install
REDIS_URL=redis://127.0.0.1:6379 node index.js
```

Default cadence:

| job         | frequency |
|-------------|-----------|
| leaderboard | 24 h      |
| markets     | 6 h       |
| trades      | 5 min     |
| positions   | 5 min     |

## Configuration

All knobs are env vars; defaults are listed in `smart_money/config.py`.

| var | purpose |
|---|---|
| `SMART_MONEY_DATABASE_URL` | PostgreSQL DSN |
| `SMART_MONEY_DATA_API_BASE` | override data-api host (defaults to `https://data-api.polymarket.com`) |
| `SMART_MONEY_GAMMA_API_BASE` | override gamma-api host |
| `SMART_MONEY_TOP_TRADER_LIMIT` | max wallets per leaderboard category (≤1000) |
| `SMART_MONEY_TRACKED_WALLET_LIMIT` | max wallets to deep-collect |
| `SMART_MONEY_ACTIVITY_LOOKBACK_DAYS` | activity & closed-positions window (default 90) |
| `SMART_MONEY_RECENT_TRADE_HOURS` | "recent" window for current bets + consensus (default 24) |
| `SMART_MONEY_MIN_CONSENSUS_TRADERS` | minimum tracked traders per consensus row (default 2) |

## API surface

| method | path | purpose |
|---|---|---|
| GET | `/api/health` | DB health probe |
| GET | `/api/dashboard` | full snapshot for the page |
| GET | `/api/top-traders?top=50` | Q1 |
| GET | `/api/market-preferences?wallets=0xabc,0xdef` | Q2 |
| GET | `/api/lead-time?wallets=...` | Q3 |
| GET | `/api/price-distribution?wallets=...` | Q4 |
| GET | `/api/current-bets?hours=24` | Q5 |
| GET | `/api/consensus` | Q6 |
| GET | `/api/trader-scores?top=50` | per-wallet profile (win rate / ROI / drawdown / composite score) |
| GET | `/api/signals?status=pass&signal_type=consensus&limit=50` | risk-filtered signals |
| GET | `/api/signals/stats` | counts by status + type |
| GET | `/api/follow-list?top=10` | wallets approved for copy-trading |
| GET | `/api/follow-orders?limit=50` | executor audit log (dry-run + live) |
| POST | `/api/follow/refresh` | manually recompute follow list + process pending signals |
| POST | `/api/collect/{leaderboard\|markets\|trades\|positions\|all}` | manual collection trigger |

## Safety boundaries

- No private keys, no CLOB signing, no order placement.
- The tracker uses only public, auth-free endpoints.
- Live data is never cached across users; you query the same tables everyone else
  would see if they ran the same collector.

## Phase 2+ — scoring, signals, risk filter, copy-trading executor

The six questions above are now layered with a *decision pipeline*:

```
trades tick (every 5min)
    │
    ├─→ scoring.py     writes smart_money_trader_scores
    │                    (win rate / ROI / drawdown / composite 0-100)
    │
    ├─→ signals.py     detects two flavours of actionable events
    │                    - new_open  (single high-score trader enters a new market)
    │                    - consensus (N+ traders agree on the same outcome)
    │
    └─→ risk.py        applies 8 filters (liquidity / expiry / price band /
                         confidence / position size / duplicate / age)
                         each signal becomes pass / shrink / block
                              │
                              ▼
follow tick (every 30s)
    │
    ├─→ followlist.py  rebuilds smart_money_follow_list
    │                    (win≥70% ROI≥100% closed≥10 score≥70 by default)
    │
    ├─→ notifier.py    POSTs Feishu card if SMART_MONEY_FEISHU_WEBHOOK_URL set
    │                    (card has 跟单 / 取消 buttons; auto-cancels in 30s)
    │
    └─→ executor.py    for each new pass+consensus signal:
                         - dry-run (default): writes FollowOrder row with status='dry_run'
                         - live (--live-trade flag): submits CLOB limit order
                           via py_clob_client, status='submitted' or 'error'
```

### Manual vs auto trading

The default mode is **dry-run**, which means every signal produces a `FollowOrder`
audit row but no real order is sent. There are three ways to actually place orders:

1. **Pure manual** — read the dashboard, click into a signal's market on
   polymarket.com, copy the suggested size, place the order yourself.
   You never expose a private key.

2. **Semi-auto (Feishu)** — set `SMART_MONEY_FEISHU_WEBHOOK_URL` to your
   Feishu bot URL. Each new pass+consensus signal arrives as a card with
   "跟单 / 取消" buttons. After `SMART_MONEY_FOLLOW_CONFIRM_TIMEOUT_SECONDS`
   (default 30s) without a click, the system auto-cancels.

3. **Fully auto (`--live-trade`)** — the executor submits a CLOB limit order
   via `py_clob_client`. You must provide a wallet key (see CLOB docs) and
   accept the full risk of automated trading. The loop logs every order in
   `smart_money_follow_orders` for audit.

```bash
# dry-run forever (default)
python -m smart_money run --job all --loop

# fully live (REAL money)
SMART_MONEY_LIVE_TRADE=1 python -m smart_money run --job all --loop --live-trade

# with Feishu confirm-before-execute
SMART_MONEY_FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/XXX \
  python -m smart_money run --job all --loop
```

### Tuning knobs (all env-overridable)

| variable | default | meaning |
|---|---|---|
| `SMART_MONEY_FOLLOW_MIN_WIN_RATE` | 0.70 | min win rate to join follow list |
| `SMART_MONEY_FOLLOW_MIN_ROI_PCT` | 100 | min ROI % to join follow list |
| `SMART_MONEY_FOLLOW_MIN_CLOSED_COUNT` | 10 | min number of closed positions |
| `SMART_MONEY_FOLLOW_MIN_SMART_MONEY_SCORE` | 70 | min composite score (0-100) |
| `SMART_MONEY_FOLLOW_TOP_N_FOR_SIGNALS` | 5 | how many top wallets drive consensus detection |
| `SMART_MONEY_FOLLOW_MIN_CONSENSUS_FOR_EXECUTE` | 3 | only auto-execute consensus with N+ traders |
| `SMART_MONEY_FOLLOW_MAX_SIZE_USDC` | 100 | max USDC per follow order |
| `SMART_MONEY_FOLLOW_CONFIRM_TIMEOUT_SECONDS` | 30 | how long to wait for Feishu button before auto-cancel |
| `SMART_MONEY_LIVE_TRADE` | 0 | 1 = submit real CLOB orders; 0 = dry-run |
| `SMART_MONEY_FEISHU_WEBHOOK_URL` | _empty_ | when set, push signal cards |

## Limitations

- The leaderboard only exposes DAY / WEEK / MONTH / ALL windows, so the "90 day"
  page tab is computed from `/closed-positions.realizedPnl` rather than from the
  leaderboard metric.
- Historical data lives in your own Postgres; there is no replay of pre-existing
  leaderboard snapshots.
- Polymarket rate limits (≈4000 req / 10 s overall) are respected via a small
  client-side throttle and exponential backoff; do not bypass it.

---

## Phase 4 — manual order entry + leader-sale detection

**Where:** `/api/follow/manual-order`, `/api/follow/detect-sales`, the *Trading*
tab in the dashboard.

**How:**
* `POST /api/follow/manual-order` — body `{condition_id, side, price, size_usdc, direction?}`.  Resolves the CLOB token from the cached `Trade.token_id` for the same market when no `asset_id` is provided.  Publishes PENDING + INFLIGHT + FILLED (or FAILED) events on the same `OrderEventBus` as the consensus executor.
* `POST /api/follow/detect-sales` — scans follow-list wallet SELL trades in the last *N* minutes (default 30) that overlap an open mirror FollowOrder; emits a `partial` event for each and pushes a Feishu card if `SMART_MONEY_FEISHU_WEBHOOK_URL` is set.
* 严格二次确认：提交按钮打开一个 modal，要求输入 `CONFIRM` 才生效。该流程不可跳过。
* 默认 size 留空 — 强制 operator 手动输入金额，防止误下大单。

**Safety:**
* 默认 `live_trade=0` ⇒ DRY-RUN；要真下单需 `SMART_MONEY_LIVE_TRADE=1`。
* 单一订单 size 限制：≥ 1 USDC、≤ 1000 USDC（超出需另开路径）。
* 仅在 follow-list 中且当前持仓镜像过同一 leader 时才发 leader-sale 告警（避免跟单名单内其他人偶然 SELL 误告）。

**Phase 5（链上对账）暂缓** — 需先走完最小下单端到端：dashboard → `/api/follow/manual-order` → SSE 看到 MIRRORED/FILLED → 一致即可进入 Phase 5。
