# First 100 Users — Growth Plan

> **4 weeks. 3 channels. 0 ad spend. Realistic with 2-3 hours/day.**

The goal is not virality. It's **targeted reach**: find the 100 Polymarket traders who are
technical enough to use a Python toolkit, active enough to trade, and frustrated enough
to pay $99 for a solution.

---

## Channel 1: Twitter / X (Weeks 1-4)

**Why:** Polymarket power users are on Twitter, not Reddit. The project is technical enough
that a code-first launch on X will resonate.

**Why it works for this product:** Traders respect code. A GitHub repo with 45 tests,
100+ commits, and honest "here's what broke me" copy is more credible than any ad.

### Week 1: Seed the conversation

**Day 1 (Monday):** Tweet 1 (the honest hook) — the "5-share CLOB rejection" story.
Don't mention pricing yet. Just the problem.

**Day 2:** Tweet 2 (code tour) — the 124 files, what each module does.

**Day 3:** Reply to 5 Polymarket traders with >1k followers. Comment on their
posts about trading infrastructure, not about Polymarket itself. Be useful.

**Day 4:** Tweet 3 (comparison: from scratch vs toolkit).

**Day 5:** Reply to r/polymarket discussions. Be genuinely helpful, not promotional.

**Day 6:** Tweet 4 (replay tool demo).

**Day 7:** Rest. Read what people are saying. Note common pain points for future content.

### Week 2: Soft launch

**Monday:** Tweet 5 (launch day).

**Tuesday-Thursday:** Reply to every person who quote-tweets or replies. Even "thanks"
increases algorithm reach. If someone asks a technical question, answer in detail — this is
your content.

**Friday:** Post a longer thread:
```
"How I would have saved 6 months if I'd had this Polymarket toolkit from day one"
— 12-tweet thread
```

### Weeks 3-4: Conversion

By now you have 20-30 engaged followers from the launch. Time to:
- Retweet the GitHub repo with "now with README" note
- Post 2x per week with tips/tricks from running the toolkit
- DM people who engaged positively (not spammy — genuine follow-up)
- Add a pinned tweet with pricing + landing page link

### Metrics to track

- Tweet impressions: aim for 5k+ on launch tweet
- Profile visits: >500 from a single tweet = good signal
- Link clicks (via bit.ly / umso): >50 from launch week = healthy
- GitHub stars: >50 by end of week 2

---

## Channel 2: Reddit (Weeks 1-2)

**Why:** r/algotrading, r/quant, and r/polymarket have the exact audience.
The trick: you can't just drop a link. You have to earn the right to post.

### Pre-work (Week 0, before launch)

Spend 7 days doing nothing but commenting on these subreddits:
- r/algotrading: 2 thoughtful comments/day on posts about crypto trading, Python, prediction markets
- r/polymarket: 1 comment/day on news/discussions
- r/quant: 1 comment/day on execution/trading systems

Goal: 100+ karma in each subreddit before posting your own content.
Mods and users check account age + karma before removing posts.

### Week 1: The educational posts

**Wednesday:** r/algotrading — the "5 gotchas" post (from marketing/content.md).
No pricing, no CTA. Just genuinely useful technical content.

**Friday:** r/polymarket — the replay tool demo post.
Link to GitHub but frame it as "I made this, here it is, no strings."

### Week 2: The product post

**Wednesday:** r/algotrading — follow-up: "Here's the toolkit that handles all 5 gotchas"
with a soft CTA to GitHub + landing page.

**Thursday:** r/quant — technical deep-dive, link to docs.

### What NOT to do

❌ "I made $X with this" (violates r/algotrading self-promotion rules)
❌ "Check out my product" (spam)
❌ Same content in multiple subs simultaneously (spam filter triggers)
❌ Posting before you have karma in the sub

### Metrics

- Post score: >20 upvotes = good (top 25% of posts)
- Comments: >5 substantive replies = the post resonated
- Subreddit mentions in Google: track with Talkwalker alerts

---

## Channel 3: Hacker News / GitHub (Week 2)

**Why HN:** High-signal traffic. If you hit the front page for 6 hours, that's 20-50k
pageviews. Even 1% converting to $99 = 10-20 sales.

**Why GitHub:** Stars + contributions = social proof.

### Show HN (best path to front page)

Post on a Tuesday or Wednesday at 9am PT (when HN's front page rotates).

```
TITLE: Show HN: I built a Polymarket trading toolkit after losing money on 5 CLOB gotchas
SUBTITLE: 45 tests, 3 prod templates, open-source engine + $99 toolkit

BODY:

Hey HN — I spent 6 months building a Polymarket trading system before learning
the hard way that Polymarket's CLOB enforces a 5-share minimum order.

The bug that cost me real money is now the first thing the system warns about.

What I built: an open-source Polymarket trading toolkit with:
- Smart-money copy trader (mirror top traders with risk caps)
- YES/NO hedging engine (pair arbitrage)
- BTC 5m fast-loop (CEX momentum → Polymarket fast markets)
- Replay tool (backtest any config on 30 days of historical data)
- Web dashboard with kill-switch

The open-source core (engine + replay + templates) is free.
The paid tier ($99 one-time) is the toolkit: production configs, docs, and support.

[GitHub link]
[Landing page link]

I'm not affiliated with Polymarket. This is infrastructure, not financial advice.
Trading on Polymarket involves real risk. I make no claims of profitability.

Questions about the architecture, the CLOB edge cases, or the trading system — AMA.
```

**Why this title works:** It has the specific technical detail ("5 CLOB gotchas") that
HN's filter favors. It doesn't use marketing speak.

### GitHub Strategy

1. Push the repo public before the Show HN post
2. Seed with 10-15 stars from your network (don't buy bots)
3. Submit to:
   - github.com/trending (if you get 50+ stars in 24h, it appears)
   - https://github.com/explore (submit as a "code sample")
   - Product Hunt (optional, Week 3-4)

4. README strategy: the first 3 lines must make a non-trader understand what it does.
   HN readers skim — they decide to click in 3 seconds.

---

## 100-User Timeline

| Week | Channel | Action | Target Reach | Expected Users |
|---|---|---|---|---|
| W1 | Twitter | 5 tweets, engagement | 3k impressions | 5-10 |
| W1 | r/algotrading | Educational post | 1k views | 3-5 |
| W1 | r/polymarket | Replay tool post | 500 views | 2-3 |
| W2 | HackerNews | Show HN | 20k views | 10-15 |
| W2 | GitHub trending | Stars + mentions | 2k views | 3-5 |
| W2 | Twitter | Thread + amp CTA | 5k impressions | 5-10 |
| W3 | r/quant | Technical post | 800 views | 2-3 |
| W3 | Twitter DM | Follow-up engaged users | 30 DMs | 3-5 |
| W3-4 | Organic | Word of mouth | — | 5-10 |
| W4 | Reddit | Follow-up case study | 500 views | 2-3 |
| **Total** | | | **~33k reach** | **40-60** |

**After W4:** If you hit 40-60 users in 4 weeks, the channel mix is working.
Double down on what drove the most. The remaining 40-60 come from:
- Organic GitHub stars (long tail)
- Word of mouth in Polymarket Discord
- Repeat visitors to landing page (retargeting via Plausible)

---

## What NOT to do

❌ **Don't buy GitHub stars** — HN detects this and it kills credibility
❌ **Don't cold DM people** — you'll get reported for spam
❌ **Don't post in 5 subreddits simultaneously** — spam filter will catch you
❌ **Don't promise results** — "I made $500/day" posts get removed everywhere
❌ **Don't ignore comments** — reply to everything for 48h after each post
❌ **Don't forget the disclaimer** — every post needs "not financial advice"

---

## 100 → 1,000 Users (Beyond MVP)

Once you have 100 paying users, the channels shift:

1. **Product Hunt launch** (free, high signal) — Week 8
2. **Affiliate program** (20% commission) — traders with audiences
3. **Polymarket community partnerships** — Discord integrations
4. **Polytrader.greg** style influencer outreach — send them the toolkit free, they post results
5. **SEO** — "polymarket trading bot python" queries take 6 months but compound

---

## Email list (critical)

Capture emails from Day 1. Even if they don't buy now, they might in 3 months.

```html
<!-- Simple email capture (add to landing page) -->
<form action="https://formspree.io/f/yourformid" method="POST">
  <input type="email" name="email" placeholder="your@email.com" required>
  <button type="submit">Get launch updates</button>
</form>
```

Use Formspree (free tier: 50 submissions/month) or Buttondown for a mailing list.
Send 1 email per week with: tips, new features, case studies (not spam).