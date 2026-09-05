# Payment & Delivery

## Platform Comparison

| | **Creem** | **Gumroad** | **Lemon Squeezy** | **USDC on Polygon** |
|---|---|---|---|---|
| **Setup time** | 5 min | 15 min | 30 min | 2 hrs |
| **Fee** | 3.9% + $0.40 | 10% + payment proc. | 5% + payment proc. | 0% |
| **Tax handling** | Built-in (VAT) | Built-in (VAT) | Built-in (MOSS/VAT) | None (buyer responsibility) |
| **License key** | Built-in | Built-in | Built-in | DIY (HMAC) |
| **Product pages** | Functional | Beautiful, customizable | Functional | N/A |
| **Customer emails** | Auto | Auto | Auto | DIY |
| **USDC support** | Yes (USDC on Polygon, 2% payout fee) | No (but can add manually) | Yes (via Convert) | Native |
| **Payout to China** | Alipay (individual) — 50k CNY/payout | ❌ (Stripe / PayPal required, mainland personal blocked) | ❌ Same as Gumroad | Native (any Polygon wallet) |
| **Refund flow** | 1-click in dashboard | 1-click in dashboard | 1-click in dashboard | DIY |
| **Recommended for** | Solo founder, mainland China payout | EU/US customers only | EU customers, serious ops | Crypto-native audience |

**Recommendation:** **Use Creem.** It's the lowest-fee option, the only one that pays out to
mainland Chinese Alipay accounts out-of-the-box, and the only one with both fiat and USDC on
Polygon for international customers. Gumroad / Lemon Squeezy are blocked for mainland Chinese
merchants (Stripe / PayPal don't support mainland individual accounts).

---

## Product Setup (Single $99 Tier)

We ship **one** product, not two — keeps the funnel simple.

### Creem product page

Go to https://www.creem.io → Sign in → Products → **Create Product**
(or edit your existing **Polymarket Trader Toolkit — $99**).

**Product: Polymarket Trader Toolkit — $99**
- Price: $99 USD
- Type: Digital product (one-time payment)
- **Delivery model: redirect to public repo + service tier.**
  Creem redirects the buyer to the public README's "Get the Toolkit"
  section and sends a Thank-you email with the Discord invite.
  **No second repo, no zip, no GitHub invite, no license key.**
  Buyer self-serves — everything they need is already on `main`.
- **Post-purchase success URL:** `<your-toolkit-repo-url>#-get-the-toolkit`
  — buyer lands back on the public README's "Get the Toolkit" section.
  (Replace `<your-toolkit-repo-url>` with the actual repo URL — set this
  value in **Creem dashboard only**, do NOT paste the URL into the
  landing page or any other public surface.)
- Tags: `polymarket`, `trading`, `toolkit`, `python`, `hedging`
- **Bonus (first 20 buyers):** Free 30-min 1-on-1 onboarding call (Zoom).
  Send the first 20 buyers a separate email with your Cal.com /
  Calendly link after each sale. After 20, stop sending — keeps it
  scarce and honest.

> **No License Key Management toggle needed.** Don't enable it. The
> code is already public on GitHub (BSL 1.1 protects it legally);
> the $99 covers the service tier (onboarding call, Discord,
> updates) — not a software gate.

### Live URL

`https://www.creem.io/payment/prod_57iXo1dPa2qTXZxw0jQ0pB`

### Embed on landing page

```html
<a href="https://www.creem.io/payment/prod_57iXo1dPa2qTXZxw0jQ0pB" class="btn">Buy the Toolkit — $99</a>
```

### Set up email automation

Creem → Product → "Thank you email" → paste:
```
Thanks for buying the Polymarket Trader Toolkit!

Everything is already on the toolkit repo — clone and run:

  git clone <your-toolkit-repo-url>
  cd poly-hedging
  pip install -r requirements.txt
  python fastloop_trader.py --dry-run   # dry-run works immediately
  python fastloop_trader.py --live      # BSL 1.1 allows production use for buyers

Discord invite: https://discord.gg/YOUR_DISCORD_LINK
Full docs: <your-toolkit-repo-url>#-get-the-toolkit

If you qualify (first 20 buyers), I'll email you separately with
onboarding-call scheduling.

Questions? Reply to this email or ping us in Discord.
```

> **Do NOT** mention a GitHub invite, license key, or runtime gate
> in this auto email — there's nothing to deliver manually. Also
> **do NOT** mention the first-20 onboarding call bonus in this
> auto email (would mislead buyers 21+). Send the bonus reply
> manually. See `commercial/admin/launch-sop.md` for the templates.

> **Placeholder substitution:** when pasting this template into Creem,
> replace `<your-toolkit-repo-url>` (both occurrences) with the actual
> repo URL. Keep the URL **out of** the public landing page,
> README, and any other user-facing surface — it lives only inside
> the Creem dashboard and this Thank-you email.

### Route sales notifications to your inbox

Creem → **Settings → Notifications** → turn on **"New sale"** → set
notification email to **liangyu6561@gmail.com**. Now every paid order
sends you an email summary with: buyer email, amount, timestamp.

**You use this to track the first-20 onboarding-call bonus.** See
[`commercial/admin/launch-sop.md`](../admin/launch-sop.md) for the
full operational runbook.

### First-20 bonus — one extra manual email, that's all

Creem's "Thank you email" goes to *every* buyer and shouldn't mention the
onboarding call (would mislead buyers 21+). Instead:

1. Creem → Products → Polymarket Toolkit → **Manual emails** → keep buyer
   emails as a list. When a new sale arrives in your inbox, check the count.
2. **If sale #1–20:** reply to that buyer manually with the Cal.com link.
3. **If sale #21+:** do nothing. They're out of the bonus pool.

---

## Lemon Squeezy Setup

### 1. Create store

Go to https://app.lemonsqueezy.com/register (free, no KYC for selling digital goods)

### 2. Add USDC as custom payment option

Lemon Squeezy natively supports card + PayPal. For USDC, add a custom field
to your checkout: "Want to pay in USDC? Email liangyu5@example.com after purchase."

This gives you the USDC wallet address to send to. Not seamless, but workable.

---

## USDC on Polygon (For Crypto-Native Customers)

### Setup (Polygon mainnet)

```bash
# 1. Generate a new wallet (cold wallet for receiving)
cast wallet new

# 2. Export only the public address (never the private key to any server)
cast wallet address --private-key 0xYOUR_PRIVATE_KEY

# Save the address: 0xAbC123...
```

### Delivery flow

1. Customer emails `pay@liangyu5.example.com` with "USDC payment for Polymarket Toolkit"
2. Reply with:
   - Amount: 99 USDC (~$99 at time of email, or fix to $99)
   - Address: `0xAbC123...` (Polygon)
   - Note: "Send exact amount. Include your email as transfer memo."
3. After confirming on-chain:
   - Send zip via WeTransfer / private GitHub release
   - Send license key via email
   - Add to Discord manually

### Pricing in USDC

Use a fixed USD amount, not a floating USDC amount (to avoid "I paid $100 when it was $99" disputes).

```python
# Rough conversion (don't use in production)
import requests
resp = requests.get("https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "usd-coin", "vs_currencies": "usd"})
usdc_price = resp.json()["usd-coin"]["usd"]  # ~1.00
usdc_amount = 99 / usdc_price  # 99 USDC
```

Or just tell customer: "Send approximately 99 USDC. I'll confirm and refund any overpayment."

### Anti-fraud note

- Always wait for 1 block confirmation before sending goods (Polygon ~2 seconds)
- Keep a spreadsheet: `email | tx_hash | amount | date | license_key | delivered`
- USDC transfers are reversible in some cases (USDC Blacklist) — wait 24h before fulfilling if amount > $500

---

## What You Ship (nothing — it's already on GitHub)

There is **no zip, no second repo, no invite, no license key** to ship.
The single public `poly-hedging` repo already contains everything.

### What the buyer gets on day 1

```
After payment, they receive:

1. (Auto) Creem Thank-you email: clone instructions + Discord invite
2. (Auto) Creem redirect: browser lands on
   `<your-toolkit-repo-url>#-get-the-toolkit`
3. (Manual, founder) Onboarding call Cal.com link (buyers 1-20 only)
```

That's the entire delivery flow. Three touchpoints:
- Creem handles 1 and 2 automatically.
- You send 3 manually for the first 20 buyers.

The buyer can immediately:
- Clone the repo
- Read all 124 Python files
- Run any dry-run / replay / calculator UI
- Run `--live` mode for production (BSL 1.1 allows this for buyers)

The $99 covers the **service tier** (onboarding call, Discord support,
strategy updates, white-label rights) — not a software gate. BSL 1.1
already protects the codebase legally (the buyer cannot, e.g.,
rebrand and resell it as a competing product).
```

---

## Refund Policy

- **14 days, no questions asked**
- Creem: process in dashboard → "Refund" button
- Lemon Squeezy: same
- USDC: ask for Polygon address → send back within 48h
- Exception: if they downloaded >3 files, consider partial refund

---

## Tax Notes (Not Financial Advice)

- Solo founder: report as hobby income (IRS) / self-employment (if >$400)
- EU customers: VAT MOSS if you sell >€10k/year to EU
- Keep records: Creem / Lemon Squeezy export CSV monthly
- Consider forming LLC if making >$10k/year (liability + tax benefits)
- **Mainland China individual merchants:** Creem handles VAT for international customers automatically. Report domestic Alipay receipts as 其他收入 on annual 个税汇算清缴.

Consult a tax professional for your specific situation.