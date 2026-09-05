# Pro Tier — Delivery Model

**Audience:** founder. One-line summary: **the single public
`poly-hedging` repo IS the product. BSL 1.1 is the protection.
Creem delivers; no zip, no invite, no license key, no runtime gate.**

(We tried a license-key runtime gate; it added complexity that no one
needed. The simpler model ships today.)

---

## How it works

```
buyer pays $99 on Creem
        │
        ├── (auto) Creem emails buyer: thank-you + Discord invite
        │
        └── (auto) Creem redirects browser to:
             <your-toolkit-repo-url>#-get-the-toolkit
                                  │
                                  ▼
              README "Get the Toolkit" section tells them:
              - "Just clone + pip install. Nothing to activate."
              - Lists what their $99 bought (onboarding, Discord, updates)
```

That's it. There is no license key, no second repo, no zip, no invite,
no runtime check. BSL 1.1 legally protects the codebase (the buyer
cannot, e.g., rebrand and resell it as a competing product — see
`LICENSE` for the Additional Use Grant).

---

## What the buyer gets for $99

The codebase is freely readable. The $99 is for the **service tier**
sitting on top of the code:

- 🎓 **1-on-1 onboarding call** — 60 min, Zoom (or 飞书会议 for
  mainland China). First 20 buyers get a free 30-min walk-through;
  all other buyers get a 60-min call included.
- 💬 **12 months of Discord support** — private channel access,
  fast turnaround on questions, bug reports, strategy tweaks
- 🆕 **6 weeks of exclusive strategy updates** — new templates,
  config refinements, replay-tool improvements pushed to Discord
  first; merged into the public repo 6 weeks later
- 🏷️ **White-label rights** (apply) — rebrand the toolkit for
  your own internal / client use; BSL 1.1 allows this for buyers
  on request (founder OK's each one)

Everything else (the code itself, dry-run mode, the calculator UI,
the replay tool, the production templates) is **already free** to
clone, read, and run.

---

## What lives in the single `poly-hedging` repo (no split)

All 124 Python files are in `main` under BSL 1.1. There is no
separate "Pro" repo.

| Path | Free / Pro | What it is |
|---|---|---|
| `core/`, `market/`, `api/`, `trading/` | Free | Config + CLOB + Data API |
| `fastloop_trader.py` | Free | BTC 5-min entry point (dry-run + live) |
| `strategy/` (base + dual-wallet event impl) | Free | Full implementations on main |
| `accounts/` | Free | Wallet contexts + multi-wallet pool |
| `smart_money/` | Free | Top-user scraping + copy-trade fan-out |
| `scheduler/`, `main/` | Free | Loop driver + cron-free heartbeat |
| `state/`, `notifications/` | Free | Live run-state + notifier interface |
| `templates/` | Free | 3 production config templates |
| `ui/hedging_calculator/` | Free | Hedging calculator web UI |
| `replay/pnl_attribution/` | Free | PnL-attribution replay tool |
| `notifiers/templates/` | Free | Feishu / Discord / Telegram templates |
| `scripts/`, `tests/` | Free | Replay, backfill, ops, dry-run regressions |
| `requirements.txt`, `config.example.json` | Free | — |

This was decided up-front to keep the model honest: what the buyer
sees in the README is what they get. No bait-and-switch, no
"production configs are in the Pro repo" copy.

---

## Why this works without a runtime gate

BSL 1.1 is a **source-available** license. It allows the buyer to:
- Read, fork, and run the code for personal or internal business use
- Modify it for their own production

It **does not** allow the buyer to:
- Resell it as a competing product
- Sub-license it to a third party

The "Additional Use Grant" section of `LICENSE` spells out the
specific non-production use cases that are allowed. This is the same
posture CockroachDB, Sentry, and HashiCorp take with their BSL
projects — none of them use a runtime gate, they rely on the
license text + the goodwill of the customer base.

The service tier ($99 = onboarding + Discord + updates) is what
people are buying. The code is the marketing surface that gets them
to the buy button.

---

## What gets shipped to the buyer

```text
1. (Automatic, Creem) buyer receives Thank-you email with Discord invite link
2. (Automatic, Creem) buyer lands on
   `<your-toolkit-repo-url>#-get-the-toolkit`
3. (Automatic, founder-side) sale-notification email lands at
   liangyu6561@gmail.com (configured in Creem Settings → Notifications)
4. (Manual, founder) send onboarding-call Cal link if order #1–20
```

There is **no second repo, no zip, no GitHub invite, no license key,
no runtime check**. Buyer self-serves.

---

## ⚠️ Don't forget before going live

- [ ] Creem product page configured (you've done this)
- [ ] Creem **Success URL** = `<your-toolkit-repo-url>#-get-the-toolkit`
- [ ] Creem **Thank-you email** template replaced with the version
      that doesn't mention a GitHub invite or license key
- [ ] Discord invite URL pasted in the Thank-you email
- [ ] `commercial/github-repo/README.md` "Get the Toolkit" section
      rewritten to reflect the no-gate model (already done)
- [ ] `commercial/landing/index.html` code-preview shows the bare
      `git clone + pip install` path with no license step (already done)
- [ ] Test purchase flow end-to-end in Creem **test mode** before
      flipping the landing page live — confirm the Success URL
      lands on the README and the Thank-you email arrives

---

## What changed vs. the previous plan

| Previous plan | Current plan |
|---|---|
| Separate `poly-hedging-pro` private repo | Single `poly-hedging` repo, everything public |
| Per-buyer GitHub collaborator invite | Nothing — buyer self-serves |
| Creem-issued license key + runtime Creem API check | None — BSL 1.1 + service tier |
| Homegrown HMAC verify | None |
| Buyer needs to copy a key into `.env` before `--live` works | Buyer just clones + runs |

The legal posture (BSL 1.1 + source-available) is **unchanged**.
The buyer UX is dramatically simpler.
