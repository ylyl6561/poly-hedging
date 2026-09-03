# Landing Page Deployment

## Deploy in 60 seconds

### Option A: Cloudflare Pages (recommended, free)

```bash
# 1. Install wrangler (one-time)
npm install -g wrangler

# 2. Login
wrangler login

# 3. Deploy
cd commercial/landing
wrangler pages deploy . --project-name=polymarket-toolkit
```

Result: `https://polymarket-toolkit.pages.dev`

To add a custom domain (`polymarket-toolkit.com`):
- Buy domain on Cloudflare Registrar (~$10/yr)
- In Pages project → Custom domains → Add

### Option B: Netlify Drop (zero CLI)

1. Go to https://app.netlify.com/drop
2. Drag the `commercial/landing/` folder onto the page
3. Get instant URL like `https://jovial-payne-12345.netlify.app`
4. Add custom domain in site settings

### Option C: Vercel CLI

```bash
npm install -g vercel
cd commercial/landing
vercel --prod
```

### Option D: GitHub Pages

```bash
# 1. Create repo: yourname.github.io
# 2. Copy commercial/landing/* to repo root
# 3. Push to main
# Result: https://yourname.github.io
```

---

## Pre-launch checklist

- [ ] Replace all `yourname` / `yourname@example.com` placeholders
- [ ] Replace `yournamespace.gumroad.com` with real Gumroad URLs
- [ ] Add `pricing/payment.md` link to footer
- [ ] Add real Discord invite link
- [ ] Take 3 dashboard screenshots, add to `/assets/`, replace hero placeholder
- [ ] Test on mobile (Chrome DevTools device mode)
- [ ] Run Lighthouse audit (aim for 95+ on all 4 categories)
- [ ] Add Google Analytics or Plausible (Plausible recommended for privacy)
- [ ] Submit to Google Search Console
- [ ] Set up Gumroad product pages (Solo + Pro tiers)
- [ ] Test Gumroad checkout end-to-end (use test mode)
- [ ] Set up email auto-responder with download link

---

## Optional enhancements

### Add a live demo GIF
Replace the code preview block with:
```html
<img src="/assets/dashboard-demo.gif" alt="Dashboard demo" style="max-width:700px;border-radius:8px;border:1px solid var(--border);">
```

Record with: `ffmpeg -f avfoundation -i "0:0" -t 10 -r 15 dashboard.gif` (macOS)

### Add Plausible analytics
```html
<script defer data-domain="polymarket-toolkit.com" src="https://plausible.io/js/script.js"></script>
```

### A/B test pricing
Use `https://www.splitbee.io/` or simple `?variant=a` query param routing.

---

## Maintenance

- Update CHANGELOG.md after each release
- Refresh dashboard screenshots quarterly
- Keep links to Gumroad / Discord working
- Add customer testimonials (with permission) as they arrive