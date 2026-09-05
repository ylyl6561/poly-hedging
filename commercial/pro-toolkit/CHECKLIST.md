# Pro Tier — Delivery & Activation Checklist

**Audience:** founder + Creem ops. This doc describes how the **single
public `poly-hedging` repo** ships a **Pro tier** without a second
private repo. (Earlier drafts planned a separate `poly-hedging-pro`
private repo; that decision was reversed — see git history.)

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
  │     buyer a         │                        │ "Get the Toolkit"
  │     LICENSE KEY     │                        ▼
  │  2. redirects buyer │              ┌──────────────────────────┐
  │     to README       │─────────────▶│ Paste license key into   │
  └─────────────────────┘              │ .env:                     │
            │                          │ POLY_PRO_LICENSE_KEY = .. │
            ▼                          └──────────────────────────┘
  ┌─────────────────────┐                        │
  │ Creem orders page   │                        │ on first run of
  │ records:            │                        │ a Pro module:
  │  - buyer email      │                        ▼
  │  - license key      │              ┌──────────────────────────┐
  │  - order ID         │              │ POST /v1/licenses/       │
  └─────────────────────┘              │ activate against         │
                                       │ Creem API → get inst_id  │
                                       │                          │
                                       │ subsequent runs:         │
                                       │ POST /v1/licenses/       │
                                       │ validate (inst_id+key)   │
                                       └──────────────────────────┘
```

**Why BSL-legal**: BSL 1.1 is source-available. The source code is on
GitHub for everyone to read. The **Additional Use Grant** restricts
**production use** of the Pro modules without a paid license key —
Creem is the issuer and validator. Dry-run and code reading stay free
forever.

---

## Creem License Key management — where the toggle is

Creem hides the License Key toggle inside the **Addons** section of
the product editor (it is NOT on the main Products list page). This
is why it's easy to miss.

### Step-by-step

1. **Creem dashboard** (`https://www.creem.io/dashboard`)
2. **Products** → click your existing **"Polymarket Trader Toolkit — $99"** product
   - if you haven't created it yet: **+ New Product** → fill out wizard → on the final step (Review) you can add addons
3. In the product editor, scroll down until you see a section labelled
   **"Addons"** (sometimes labelled **"Advanced"** depending on Creem's
   current UI). Inside it:
   - ☑ **License Key Management** — turn ON
   - **Activation limit:** `3` (devices per buyer; lets them move
     between laptop / desktop / VPS without contacting you)
   - **Expiration period:** `Never expires` (one-time purchase,
     no subscription)
4. Click **Save**. The license key gets emitted automatically per order
   from now on.

### Reference

If the UI changes, the official guide is here:
- Docs: https://docs.creem.io/features/addons/licenses
- The toggle lives at the same place as "Trial" / "File Downloads" /
  "Private Notes" addons — they're all in the same section.

---

## What lives where (no repo split needed)

The single `poly-hedging` repo's `main` branch contains **everything**.

| Path | Default | Becomes Pro when |
|---|---|---|
| `core/`, `market/`, `api/`, `trading/` | Free | (always free) |
| `fastloop_trader.py` | Free | `--live` mode gates on license |
| `strategy/` (base + dual-wallet event impl) | Free | production executor gates on license |
| `accounts/` | Free | real-wallet signing paths gate on license |
| `smart_money/` | Free | scrape+fan-out production mode gates on license |
| `scheduler/`, `main/` | Free | live-loop driver gates on license |
| `state/` | Free | live run-state writes gate on license |
| `notifications/` | Free | live-channel senders gate on license |
| `templates/` | Free | 3 production configs ship with the repo; live `--live` flag in each config gates on license |
| `ui/hedging_calculator/` | Free | calculator works without license; "Export to Pro config" requires license |
| `replay/pnl_attribution/` | Free | replay works without license; live mode is already covered by `fastloop_trader.py --live` gate |
| `notifiers/templates/` | Free | production notifier templates ship with the repo |
| `tests/` | Free | all tests pass without license (license mock in conftest) |
| `scripts/`, `requirements.txt`, `config.example.json` | Free | — |

The free reader can browse everything, run any dry-run, replay any
journal, use the calculator — they just can't put real money into the
Pro paths. That's the BSL + Creem-License boundary.

---

## What gets shipped to the buyer

```text
1. (Automatic, Creem) buyer receives license-key email with the key
2. (Automatic, Creem) buyer lands on
   https://github.com/ylyl6561/poly-hedging#-get-the-toolkit
3. (Automatic, founder-side) sale-notification email lands at
   liangyu6561@gmail.com (configured in Creem Settings → Notifications)
4. (Manual, founder) send onboarding-call Cal link if order #1–20
```

There is **no second repo, no zip, no GitHub invite**. Buyer self-serves.

---

## Buyer activation flow (what they see on the README)

After payment, buyer lands on the README's "Get the Toolkit" section and
sees this:

```text
1. (Already in your inbox) Find the license-key email from Creem.
   Copy the key — looks like ABC123-XYZ456-XYZ456-XYZ456.
2. Clone the public repo:
       git clone https://github.com/ylyl6561/poly-hedging.git
       cd poly-hedging
3. Add the license key to .env:
       echo "POLY_PRO_LICENSE_KEY=YOUR-KEY-HERE" >> .env
4. (You need CREEM_API_KEY in your .env too. The CREEM_API_KEY is a
   server-side secret the FOUNDER gave you — they don't share their
   own key, but they pin a public Creem API client secret in the
   public repo's .env.example so Pro users can validate.)
   — TBD: see "How license validation works" below for the actual
     flow; the README will spell this out precisely once you've chosen.
5. Run any Pro module — it will verify the key via Creem API and unlock:

       python fastloop_trader.py --live                # BTC 5m FastLoop
       python smart_money/run_copy_trader.py --live    # top-user mirror
       python scheduler/run_dual_wallet_hedge.py --live

   Without the key these commands exit with:
       RuntimeError: POLY_PRO_LICENSE_KEY not set — see README "Get the Toolkit"

Dry-run stays free — every entry point defaults to --dry-run.
```

---

## How license validation actually works (Creem API)

We use **Creem's License API** (not a homegrown HMAC). This means:
- License keys are **server-issued** by Creem — we can't forge them.
- Validation can be **revoked** from the Creem dashboard (refund → key
  status flips to `disabled` → next `validate` call rejects).
- Activation limit is tracked **server-side** (a buyer's 4th device
  will get HTTP 403 from `/v1/licenses/activate`).

### Endpoints we call

```
POST https://api.creem.io/v1/licenses/activate
     body: {"key": "<license>", "instance_name": "<unique-id>"}
     → returns {"id": "<license-instance-id>", "status": "active", ...}

POST https://api.creem.io/v1/licenses/validate
     body: {"key": "<license>", "instance_id": "<license-instance-id>"}
     → returns {"status": "active" | "inactive" | "expired" | "disabled"}
```

(For test mode, prepend `test-` to the subdomain: `test-api.creem.io`.)

### `tools/verify_license.py` (the runtime gate)

```python
# tools/verify_license.py
"""Creem License API client + Pro-tier gate."""
import os
import sys
import json
import platform
import uuid
import requests

CREEM_API_BASE = os.environ.get(
    "CREEM_API_BASE", "https://api.creem.io"
)
# Founder provides this. Also embedded in .env.example with a
# documented value for the public test mode.
CREEM_API_KEY = os.environ.get("CREEM_API_KEY", "")

# Local cache of (license_key, instance_id) so we don't hit the API
# on every CLI invocation (avoids rate limits + offline-friendly).
_CACHE_PATH = os.path.expanduser("~/.poly_hedging_license.json")


def _instance_name() -> str:
    """Stable per-machine identifier (no PII)."""
    return f"{platform.node()}-{uuid.getnode()}"


def activate(license_key: str, instance_name: str | None = None) -> dict:
    """First-time activation. Stores instance_id locally for reuse."""
    url = f"{CREEM_API_BASE}/v1/licenses/activate"
    payload = {"key": license_key, "instance_name": instance_name or _instance_name()}
    resp = requests.post(
        url, json=payload,
        headers={"x-api-key": CREEM_API_KEY, "accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    _save_cache(license_key, data.get("id", ""), data.get("instance", [{}])[0].get("id", ""))
    return data


def validate(license_key: str | None = None, instance_id: str | None = None) -> bool:
    """Returns True iff Creem says the license is currently active."""
    if license_key is None:
        license_key = os.environ.get("POLY_PRO_LICENSE_KEY", "").strip()
    cached = _load_cache(license_key)
    if instance_id is None:
        instance_id = cached.get("instance_id", "") if cached else ""
    if not license_key or not instance_id:
        return False
    url = f"{CREEM_API_BASE}/v1/licenses/validate"
    try:
        resp = requests.post(
            url,
            json={"key": license_key, "instance_id": instance_id},
            headers={"x-api-key": CREEM_API_KEY, "accept": "application/json"},
            timeout=10,
        )
    except requests.RequestException:
        # Network error: trust the last cache (offline grace period)
        return bool(cached and cached.get("active"))
    if resp.status_code != 200:
        return False
    data = resp.json()
    return data.get("status") == "active"


def require_pro_license() -> None:
    """Import-side guard for Pro modules. Raise if no active license."""
    if "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1":
        return  # dry-run path stays free
    key = os.environ.get("POLY_PRO_LICENSE_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "POLY_PRO_LICENSE_KEY not set — see README #-get-the-toolkit "
            "to activate the Pro tier."
        )
    # First run on a new machine: activate. Subsequent: validate.
    cached = _load_cache(key)
    if cached is None or not cached.get("active"):
        try:
            activate(key)
        except Exception as e:
            raise RuntimeError(f"Failed to activate license: {e}") from e
    if not validate(key):
        raise RuntimeError(
            "License not active (expired, revoked, or device limit "
            "reached). Visit Creem dashboard → Orders to check, or "
            "contact founder for a reset."
        )


# --- local cache helpers (tiny JSON, no PII) -------------------------

def _save_cache(license_key: str, license_id: str, instance_id: str) -> None:
    payload = {"license_id": license_id, "instance_id": instance_id, "active": True}
    try:
        with open(_CACHE_PATH, "w") as fh:
            json.dump({license_key: payload}, fh)
        os.chmod(_CACHE_PATH, 0o600)
    except OSError:
        pass  # read-only FS; just revalidate on every run


def _load_cache(license_key: str) -> dict | None:
    try:
        with open(_CACHE_PATH) as fh:
            data = json.load(fh)
        return data.get(license_key)
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        key = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
            "POLY_PRO_LICENSE_KEY", ""
        )
        print("✓ Active" if validate(key) else "✗ Not active")
    elif cmd == "activate":
        key = sys.argv[2] if len(sys.argv) > 2 else os.environ.get(
            "POLY_PRO_LICENSE_KEY", ""
        )
        if not key:
            print("POLY_PRO_LICENSE_KEY not set", file=sys.stderr)
            sys.exit(2)
        print(json.dumps(activate(key), indent=2))
```

### `tools/__init__.py`

```python
# tools/__init__.py
from .verify_license import require_pro_license, validate, activate
```

### Pro modules start with

```python
# accounts/__init__.py
from tools import require_pro_license

def _gate_live_mode():
    """Called only when caller passes --live. Dry-run stays free."""
    require_pro_license()
```

---

## `CREEM_API_KEY` — the founder's side

| Where it goes | What value |
|---|---|
| Founder's local `.env` (gitignored) | Founder's real Creem API secret (test-mode + live-mode keys) |
| CI (`GitHub Actions → Settings → Secrets`) | Same key (server-side only) |
| `config.example.json` / `.env.example` | **No real key** — just a placeholder comment so Pro users understand they need to ask for it |

⚠️ **Two options for distributing CREEM_API_KEY to Pro users** — pick one:

**Option A (simpler — picked for v1):** Founder ships **one**
public Creem API key in the repo's `.env.example` (a "validate-only"
client key Creem will issue for this). Pro users copy that key into
their own `.env`. The same key can validate any license, so it's
fine that everyone has it.

**Option B (paranoid — for v2):** Pro users paste their license key
into a CLI helper (`./scripts/activate_pro.sh`) which then calls back
to a founder-controlled webhook (e.g. `https://liangyu5.example.com/
api/validate`) that proxies to Creem. Founder proxies — buyers never
hold the Creem API key. More moving parts; recommended only if a
buyer's misuse of the Creem API is a real concern.

**v1 ships Option A** (see `.env.example` below).

### `.env.example` — what gets committed

```bash
# === License & Pro tier ===
# Both are required for --live mode on any Pro module.
# Dry-run never needs these.
#
# POLY_PRO_LICENSE_KEY comes from your Creem purchase-receipt email
# (e.g. ABC123-XYZ456-XYZ456-XYZ456). One per buyer.
POLY_PRO_LICENSE_KEY=

# CREEM_API_KEY is the founder's public validate-only Creem API key,
# set so buyers can validate their license against Creem. The same
# value is committed in .env.example on the public repo — that's
# expected and safe; it can only validate, not create, keys.
CREEM_API_KEY=creem_pub_validate_xxxxxxxxxxxxxxxxxxxx
```

---

## ⚠️ Don't forget before going live

- [ ] Creem product **Addons → License Key Management** turned ON,
      activation limit = 3, expiration = Never
- [ ] Founder has the **public validate-only** Creem API key, baked
      into the repo's `.env.example`
- [ ] Founder has the **secret write-side** Creem API key in the
      founder's local `.env` (and in CI as a GitHub Actions secret)
- [ ] `commercial/github-repo/README.md` "Get the Toolkit" section
      matches the flow above (already done)
- [ ] `commercial/landing/index.html` code-preview shows
      `git clone https://github.com/ylyl6561/poly-hedging.git`
      (already done)
- [ ] End-to-end test in Creem **test mode** before flipping the
      landing page live — buy the product with the test card, confirm
      you receive a license-key email, paste into `.env`, run
      `python tools/verify_license.py check` and see "✓ Active"

---

## What changed vs. the previous plan

| Previous plan | Current plan |
|---|---|
| Separate `poly-hedging-pro` private repo | Single `poly-hedging` repo, everything public |
| Per-buyer GitHub collaborator invite | Per-buyer license key (auto-emailed by Creem) |
| Manual invite step (founder sends it within 24h) | Self-served (founder does nothing; Creem delivers) |
| Homegrown HMAC verify (offline) | Creem License API (online, with offline cache) |
| License key embedded in repo as proof-of-purchase | License key is the runtime gate |

The legal posture (BSL 1.1 + source-available + paid-production-use
boundary) is **unchanged**. Creem's License API is the same model
CockroachDB / Sentry / many BSL projects use, just with a hosted
issuer instead of self-hosted HMAC.
