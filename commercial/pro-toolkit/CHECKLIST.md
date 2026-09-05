# Pro Repo — CHECKLIST

This is the founder's checklist for what lives in the **private** `poly-hedging-pro`
repo vs. the **public** `poly-hedging` repo. It's a planning doc, not source code.

---

## One-time: create the private repo

```bash
gh repo create ylyl6561/poly-hedging-pro \
  --private \
  --description "Polymarket Trader Toolkit — paid tier (private)"
```

---

## What goes in the Pro repo (everything currently in the public repo's working tree)

The public `poly-hedging` repo stays as the open-core framework only.
**Everything below — currently public — moves to Pro.**

### Production strategy modules (must be Pro-only)

| Path | Why Pro |
|---|---|
| `strategy/dual_wallet_event_strategy.py` | Full implementation, multi-wallet |
| `strategy/dual_wallet_executor.py` | CLOB execution with relayer redeem |
| `strategy/dual_wallet_models.py` | Event + leg + result dataclasses |
| `strategy/account_pool.py` | Multi-wallet rotation, secrets |
| `accounts/` | Wallet contexts, signing keys, per-account state |
| `smart_money/` | Top-user scraping, copy-trade fan-out |
| `scheduler/`, `main/` | Live loop driver |
| `state/` | Live run state, candidate journals |
| `notifications/` | Production notifiers |

### Pro-only additions (don't exist yet, write them)

| Path | What it is |
|---|---|
| `templates/btc_5m_fastloop.json` | BTC 5-min FastLoop production config |
| `templates/smart_money_copy.json` | Smart-money copy trader production config |
| `templates/dual_wallet_hedge.json` | Dual-wallet hedge production config |
| `ui/hedging_calculator/` | Hedging calculator web UI (Flask or Vite+React) |
| `replay/pnl_attribution/` | PnL-attribution replay tool |
| `notifiers/templates/feishu.json` | Feishu notifier template |
| `notifiers/templates/discord.json` | Discord notifier template |
| `notifiers/templates/telegram.json` | Telegram notifier template |

### What stays in the **public** repo (free, open-core)

| Path | Why free |
|---|---|
| `core/` | Pure config-resolution helpers, no secrets |
| `market/` | CLOB order-book utilities, generic |
| `trading/`, `api/` | CLOB SDK wrapper, read-mostly Data API client |
| `scripts/run_fastloop_path_score.sh` | Dry-run observation script |
| `tests/` | Replay tests + dry-run regressions (no live fixtures) |
| `config.example.json` | Empty-template example |
| `requirements.txt` | Public dependencies |
| `README.md`, `LICENSE` | Public docs |
| `fastloop_trader.py` (dry-run mode only) | CLI for dry-run; live mode requires Pro configs |

---

## Two-step migration plan

### Step 1 — From the public repo, extract the Pro list to a branch

```bash
cd /Users/yuliang/poly/poly-hedging
git checkout -b pro-migration

# Delete the Pro-only paths from this branch's tree
git rm -r accounts/ strategy/ smart_money/ scheduler/ main/ state/ notifications/

# Rewrite `strategy/` and the CLI to refuse live mode (no Pro configs available)
# This is a code change — keep dry-run path intact, but error if --live is passed.

git commit -m "split: extract Pro-only modules (migrated to poly-hedging-pro)"
git push origin pro-migration
```

### Step 2 — Create the Pro repo from the original `main`

```bash
# Clone the original main as poly-hedging-pro
git clone https://github.com/ylyl6561/poly-hedging.git poly-hedging-pro
cd poly-hedging-pro
git remote set-url origin git@github.com:ylyl6561/poly-hedging-pro.git

# Add the new Pro-only additions (templates/, ui/, replay/, notifiers/templates/)
git add templates/ ui/ replay/ notifiers/templates/
git commit -m "pro: add production templates + UI + replay"
git push -u origin main

# Now flip the public repo's default branch to pro-migration:
gh repo edit ylyl6561/poly-hedging --default-branch pro-migration
gh repo edit ylyl6561/poly-hedging --delete-branch main
```

---

## Per-buyer invite (the actual delivery)

See [`commercial/admin/launch-sop.md` § 4](../admin/launch-sop.md).

```bash
gh repo invite-user ylyl6561/poly-hedging-pro --user <buyer_github_username>
```

---

## ⚠️ Don't forget before going live

- [ ] `poly-hedging-pro` repo exists and is private
- [ ] `poly-hedging` repo's default branch flipped to `pro-migration`
- [ ] Public repo's README "Get the Toolkit" link points to Creem (already done)
- [ ] Creem success URL = `https://github.com/ylyl6561/poly-hedging#-get-the-toolkit`
- [ ] Creem sales notification email = `liangyu6561@gmail.com`
- [ ] Test purchase flow end-to-end (Creem test mode) before flipping branch
