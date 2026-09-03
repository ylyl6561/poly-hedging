# Marketing Content Templates

## Twitter / X

### How to use
- Replace `[YOUR_HANDLE]`, `[TOOLKIT_LINK]`, `[GITHUB_LINK]` placeholders
- Schedule with Buffer / Later, not posted all at once
- Each tweet: post, then reply to it 2x in the next hour to boost algorithm reach
- Use a tool like `twikit.com` or `charm.sh` for quick quote-tweet variants

---

## Tweet 1: The Honest Hook (Day 1, Week 1)

> I spent 6 months building a Polymarket trading system before I learned what a "5-share minimum order rejection" was.
>
> The bug that cost me real money is now the first thing the system warns about.
>
> Open-sourcing the engine today → [TOOLKIT_LINK]
>
> (The paid playbook has 3 templates that would have saved me $2k+)

---

## Tweet 2: The Code Tour (Day 2, Week 1)

> What 124 Python files look like when you build a Polymarket trading desk from scratch:
>
> 📊 copy trader
> 🛡️ hedging engine
> ⚡ BTC 5m fast-loop
> 🔁 replay tool
> 🌐 dashboard
>
> Open-source core, paid playbook: [GITHUB_LINK]
>
> 45 tests, 100+ commits, 0 magic wands. Just the code.

---

## Tweet 3: The Comparison (Day 3, Week 1)

> Building a Polymarket trading bot: vs using an existing toolkit.
>
> **From scratch:**
> - 200+ hours to production
> - 17 bugs before it stops breaking
> - $0 upfront, unlimited regret
>
> **With polymarket-toolkit:**
> - 30 min to first dry-run
> - same bugs already fixed
> - $49 one-time
>
> [TOOLKIT_LINK]

---

## Tweet 4: The Replay Hook (Day 4, Week 1)

> Every trading strategy looks good on paper.
>
> Run it through 30 days of replay first.
>
> Our replay tool: $ git ./scripts/replay.sh config.json --days 30
> Output: 412 fills | 67% win rate | +$842 PnL (paper)
>
> Now you know before you risk real money.
>
> Get the toolkit → [TOOLKIT_LINK]

---

## Tweet 5: The Launch / Proof (Launch Day)

> We shipped it.
>
> Polymarket Trader Toolkit — open-source engine + 3 production templates.
>
> Built because we lost money first. Tested because we run it ourselves.
>
> Solo $49 / Pro $129
>
> GitHub: [GITHUB_LINK]
> Landing: [TOOLKIT_LINK]
>
> First 48 hours: 20% off with code FIRST48

---

## Bonus: Quote-Tweet Template (use with every tweet)

> The template for CTAs:
> "I wish I'd had this 6 months ago." — [a real person, after they buy]
>
> Save this tweet. When you build your own trading system and hit your first Polymarket CLOB edge case, come back.

---

## Reddit

### How to use
- Always engage in the community FIRST, post second
- Spend 1-2 weeks commenting on r/polymarket, r/algotrading, r/quant before posting
- Use a fresh account or one with >100 karma in the sub
- No direct "buy now" — lead with the tool itself

---

### Reddit Post 1: r/algotrading (Educational, Week 1)

```
TITLE: I built a Polymarket copy-trader that mirrors top traders automatically.
 Here's what the 5-share CLOB minimum taught me the hard way.

BODY:

Hey r/algotrading — wanted to share what I've been working on for the past 6 months.

**The problem I was solving:**

I wanted to auto-mirror Polymarket's most profitable traders (like what whale trackers do on Binance).
 Sounds straightforward, right? Fill price > $X, replicate size, GTC sell at profit threshold.

**What Polymarket's CLOB actually does to you:**

```
# Every low-min attempt hits this wall
ValueError: min_usdc must be >= $0.50
(Polymarket CLOB enforces a 5-share minimum order)
```

At certain prices, 5 shares = $0.50. If your min_usdc = $0.49, it rejects. If your leader's
fill price is unpredictable, you can't pre-validate. So you get either false alerts or silent
rejections — and the leader goes to $1 profit while you're still debugging.

The fix: check price * 5 >= min_usdc at fill time, not at order time. Warn rather than reject.
 Emit a "minimum 5 shares requires at least $X" alert. Don't block the whole trade.

**The full system (now open-source):**

- Smart-money copy trader with tier-based take-profit
- Hedging engine for YES/NO pairs
- BTC 5m fast-loop (CEX momentum → Polymarket fast)
- Replay tool (backtest any config on historical data)
- Dashboard with kill-switch

GitHub: [GITHUB_LINK]

**Honest caveats:**

- This is infrastructure, not a signal service
- I make no claims of profitability
- Polymarket is restricted in some jurisdictions
- The US, UK, EU may have regulations that apply to you

Questions welcome. If you've run into similar CLOB gotchas, I'd love to hear what tripped you up.

AMA.

---

### Reddit Post 2: r/polymarket (Product-focused, Week 1-2)

```
TITLE: Built a free replay tool for Polymarket BTC fast-markets.
 Paste your config, see 30 days of paper fills before risking anything.

BODY:

x-post from r/algotrading

Hey r/polymarket — made a replay tool specifically for Polymarket BTC fast-markets.

**What it does:**

```
$ ./scripts/replay.sh config.json --days 30
[replay] 412 fills | 67% win rate | +$842 PnL (paper)
```

You point it at a config file, it pulls historical Polymarket data, simulates your strategy,
and spits out a fill log with PnL.

**Why I built it:**

I kept deploying strategies that looked great on paper and then blew up in live trading.
 Turns out Polymarket's CLOB has edge cases (spread, slippage, 5-share minimum)
 that don't show up in backtests unless you simulate fills properly.

**The toolkit:**

The replay tool is part of a larger open-source project:
[GITHUB_LINK]

Solo tier ($49) gets you 3 production config templates + the replay tool.
 Pro ($129) adds PnL attribution, hedging calculator, and onboarding.

Again: this is a tool, not financial advice. Check your local regulations.

---

### Reddit Post 3: r/quant (Technical deep-dive, Week 2)

```
TITLE: Polymarket CLOB order execution: 5 gotchas that will cost you money if you ignore them.
 Includes mitigation code.

BODY:

After 6 months of running Polymarket trading strategies live, here's what actually breaks:

**1. The 5-share minimum (most common)**

Polymarket's CLOB requires ≥5 shares per order. At sub-cent prices, this means
a minimum USDC value per order. If you're trying to copy a tiny leader position,
your order silently fails.

Mitigation: `max(min_usdc, fill_price * 5)`

**2. Fill price != displayed price**

Slippage on fast-markets can be 1-3 cents. Displayed price = 0.45¢, filled at 0.48¢.
 Your take-profit tier fires 3 cents too late.

Mitigation: track realized fill price, not order price.

**3. GTC orders on resolved markets**

Polymarket resolves markets. GTC (Good Till Cancel) orders on resolved markets get
 cancelled automatically, but not synchronously. You can get fills after resolution
 in some edge cases.

Mitigation: market state polling before order acceptance.

**4. YES/NO pair pricing doesn't sum to 1**

Unlike binary options, Polymarket YES + NO can trade at $0.97 + $0.04 = $1.01.
 This breaks the naive hedge calculation.

Mitigation: use mid-price, not last-trade price, for spread calculation.

**5. The leader's fill price is unknowable pre-fill**

You can't pre-validate "can I replicate this trade?" because you don't know the leader's
 actual fill price until after it's filled. You can only check price * 5 >= min_usdc
 as a necessary but insufficient condition.

Mitigation: warn on price near threshold, accept that some fills will fail.

**Tool that handles all of this:**

I open-sourced the system here: [GITHUB_LINK]
Docs on hedging here: [TOOLKIT_LINK]

No monetization agenda here — just saving you the 3 weeks I spent figuring this out.
 AMA on Polymarket execution specifics.
```

---

## Timing calendar (Week 1)

| Day | Platform | Action |
|---|---|---|
| Mon | Twitter | Tweet 1 (the honest hook) |
| Tue | Twitter | Tweet 2 (code tour) |
| Wed | r/algotrading | Post 1 (educational) |
| Thu | Twitter | Tweet 3 (comparison) |
| Fri | r/polymarket | Post 2 (product) |
| Sat | Twitter | Tweet 4 (replay hook) |
| Sun | — | Rest, engage comments |
| Next Mon | Twitter | Tweet 5 (launch day) |
| Next Wed | r/quant | Post 3 (technical) |