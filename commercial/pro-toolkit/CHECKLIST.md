# Pro Tier — Delivery & Activation Checklist

**Audience:** founder + Creem ops. This doc describes how the **single
public `poly-hedging` repo** ships a **Pro tier** without a second private
repo. (Earlier drafts planned a separate `poly-hedging-pro` private repo;
that decision was reversed — see git history.)

---

## Architecture (the new model)

```
                                       ┌────────────────────────┐
   buyer pays $99 on Creem             │ github.com/ylyl6561/   │
            │                          │ poly-hedging (PUBLIC)  │
            ▼                          │ main = everything      │
  ┌─────────────────────┐              └────────────────────────┘
  │ Creem webhook fires │                        │
  │  1. auto-emails     │                        │ buyer reads README
  │     buyer the       │                        │ "Get the Toolkit"
  │     LICENSE KEY     │                        ▼
  │  2. redirects buyer │              ┌──────────────────────┐
  │     to README       │─────────────▶│ Paste license key    │
  └─────────────────────┘              │ into .env:           │
                                       │ POLY_PRO_LICENSE_KEY │
                                       │ = xxx-xxx-xxx.xxx    │
                                       └──────────────────────┘
                                                  │
                                                  ▼
                                       Runtime check in
                                       accounts/, smart_money/,
                                       scheduler/, etc.
                                       unlocks Pro mode.
```

**Why BSL-legal**: BSL 1.1 is source-available. The source code is on
GitHub for everyone to read. The **Additional Use Grant** restricts
**production use** of the Pro modules without a paid license key. Dry-run
and code reading are free for everyone, just like CockroachDB / Sentry
BSL projects.

---

## What lives where (no repo split needed)

The single `poly-hedging` repo's `main` branch contains **everything**.

| Path | Default | Becomes Pro when |
|---|---|---|
| `core/`, `market/`, `api/`, `trading/` | Free | (always free) |
| `fastloop_trader.py` | Free | `--live` mode gates on license |
| `strategy/` (base framework + dual-wallet event impl) | Free | production executor gates on license |
| `accounts/` | Free | real-wallet signing paths gate on license |
| `smart_money/` | Free | scrape+fan-out production mode gates on license |
| `scheduler/`, `main/` | Free | live-loop driver gates on license |
| `state/` | Free | live run-state writes gate on license |
| `notifications/` | Free | live-channel senders gate on license |
| `templates/` | Free | 3 production configs ship with the repo; the **HMAC-encrypted live-mode flag** inside each config gates on license |
| `ui/hedging_calculator/` | Free | the calculator works without license; only "Export to Pro config" requires license |
| `replay/pnl_attribution/` | Free | replay works without license; live mode is already covered by `fastloop_trader.py --live` gate |
| `notifiers/templates/` | Free | production notifier templates ship with the repo |
| `tests/` | Free | all tests pass without license (license mock in conftest) |
| `scripts/`, `requirements.txt`, `config.example.json` | Free | — |

The free reader can browse everything, run any dry-run, replay any
journal, use the calculator — they just can't put real money into the
Pro paths. That's the BSL + license-key boundary.

---

## What gets shipped to the buyer

```text
1. (Automatic, Creem) buyer receives license-key email
2. (Automatic, Creem) buyer lands on https://github.com/ylyl6561/poly-hedging#-get-the-toolkit
3. (Automatic, optional) founder receives sale-notification email
4. (Manual, founder) send onboarding-call Cal link if order #1–20
```

There is **no second repo, no zip, no GitHub invite**. Buyer self-serves.

---

## Creem product configuration (one-time)

Go to https://www.creem.io → Sign in → Products → **Create Product**

**Product: Polymarket Trader Toolkit — $99**
- Price: $99 USD
- Type: Digital product (one-time payment)
- **Delivery model: License key + redirect.** Creem auto-generates a unique
  license key per order and emails it to the buyer. No manual repo invite.
- **Issue license keys:** ✅ Enabled (use Creem's built-in license-key
  generator — these keys map to the buyer's order in the Creem dashboard).
- **License-key activation mode:** buyer pastes the key into
  `POLY_PRO_LICENSE_KEY=` in `.env` (see `commercial/pricing/payment.md`).
- **Post-purchase success URL:**
  `https://github.com/ylyl6561/poly-hedging#-get-the-toolkit`
  — buyer lands back on the public README, sees the activation steps.
- Tags: `polymarket`, `trading`, `toolkit`, `python`, `hedging`
- **Bonus (first 20 buyers):** Free 30-min 1-on-1 onboarding call (Zoom).
  Send the first 20 buyers a separate email with your Cal.com link
  after each sale (see `commercial/admin/launch-sop.md`).

### Live URL
`https://www.creem.io/payment/prod_57iXo1dPa2qTXZxw0jQ0pB`

---

## Buyer activation flow (what they see on the README)

After payment, buyer lands on the README's "Get the Toolkit" section and
sees this:

```text
1. (Already in your inbox) Find the license-key email from Creem.
2. Clone the public repo:
       git clone https://github.com/ylyl6561/poly-hedging.git
       cd poly-hedging
3. Add the license key to .env:
       echo "POLY_PRO_LICENSE_KEY=YOUR-KEY-HERE" >> .env
4. (Optional) drop your wallet private key + RPC URLs into .env.
5. Run any Pro module — it will verify the key locally and unlock:

       # Examples (Pro paths that gate on the license):
       python fastloop_trader.py --live                # BTC 5m FastLoop
       python smart_money/run_copy_trader.py --live    # top-user mirror
       python scheduler/run_dual_wallet_hedge.py --live

   Without the key these commands exit with:
       RuntimeError: POLY_PRO_LICENSE_KEY not set — see README "Get the Toolkit"

Dry-run stays free — every entry point defaults to --dry-run.
```

---

## License-key verification (how the gate works)

`tools/verify_license.py` — the same HMAC check the README points to:

```python
# tools/verify_license.py
import hmac, hashlib, os, sys

# Same secret used to mint keys (Creem embeds it in every key they emit).
# Kept here as a fallback so tests / CI work without env var.
LICENSE_SECRET = os.environ.get(
    "POLY_LICENSE_SECRET",
    "dev-secret-do-not-use-in-prod",
)

def verify_license(license_key: str) -> bool:
    """Verify a Creem-style license key: PREFIX-XXXX-XXXX.YYY"""
    if "." not in license_key or "-" not in license_key:
        return False
    prefix, provided_hash = license_key.rsplit(".", 1)
    expected = hmac.new(
        LICENSE_SECRET.encode(), prefix.encode(), hashlib.sha256
    ).hexdigest()[:32]
    return hmac.compare_digest(expected, provided_hash)

if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "POLY_PRO_LICENSE_KEY", ""
    )
    print("✓ Valid" if verify_license(key) else "✗ Invalid")
```

`tools/__init__.py` re-exports this as `require_pro_license()`:

```python
# tools/__init__.py
from .verify_license import verify_license

def require_pro_license() -> None:
    """Import-side guard for Pro modules. Raise if no valid key."""
    import os
    key = os.environ.get("POLY_PRO_LICENSE_KEY", "").strip()
    if not key or not verify_license(key):
        raise RuntimeError(
            "POLY_PRO_LICENSE_KEY missing or invalid — see README "
            "#-get-the-toolkit to activate Pro tier."
        )
```

Pro modules start with:
```python
# accounts/__init__.py
from tools import require_pro_license

def _require_pro_for_live():
    """Called only when caller passes --live. Dry-run path stays free."""
    import os, sys
    if "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1":
        return
    require_pro_license()
```

So the dry-run / replay / read-the-code paths never ask for a key. Only
real-money paths gate it. This keeps the free user-experience genuine.

---

## ⚠️ Don't forget before going live

- [ ] Creem product "Issue license keys" turned on
- [ ] `POLY_LICENSE_SECRET` set in your local `.env` (also in CI as a
      GitHub Actions secret for tests). Creem embeds this in every key it
      emits, so the secret **must match** between you and your CI.
- [ ] `commercial/github-repo/README.md` "Get the Toolkit" section
      matches the flow above (already done)
- [ ] `commercial/landing/index.html` "Get the Toolkit" CTA links to
      Creem; pricing card reflects single-tier $99 (already done)
- [ ] `commercial/landing/index.html` code-preview shows
      `git clone https://github.com/ylyl6561/poly-hedging.git` (already
      done — was `poly-hedging-pro` previously)
- [ ] Test purchase flow end-to-end in Creem test mode before flipping
      the landing page live

---

## What changed vs. the previous plan

The old plan: separate `poly-hedging-pro` private repo, invite each
buyer as collaborator, send clone instructions.

Why we switched: single-repo is simpler for both sides — buyer
self-serves in <1 minute, founder doesn't send invites, no separate
codebase to keep in sync. BSL 1.1 already permits source-visible +
paid-runtime model (CockroachDB, Sentry, HashiCorp all do this), so
the legal posture is unchanged.
