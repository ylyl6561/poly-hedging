# Polymarket Trader Toolkit

[![License: BSL 1.1](https://img.shields.io/badge/License-BSL_1.1-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-45%2B%20passing-brightgreen)](tests/)
[![Discord](https://img.shields.io/discord/placeholder?label=Discord)](https://discord.gg/placeholder)

> **Open-source engine, paid playbook.** Run your own Polymarket trading desk without writing 5,000 lines of boilerplate.

This is the **open-source core** of the Polymarket Trader Toolkit. It includes the engine, replay tools, and three production-tested config templates.

For the **Pro playbook** (hedging calculator UI, PnL attribution, 1-on-1 onboarding, 6-week exclusive updates) → see [pricing](#-pricing).

---

## ⚠️ Important Disclaimer

**This software is provided for educational and research purposes only.** It is not financial advice. Trading on Polymarket involves substantial risk of loss. The maintainers:

- Make **no claims of profitability** or ROI
- Are **not registered** as investment advisers or broker-dealers in any jurisdiction
- Accept **no liability** for trading losses incurred using this software

You are solely responsible for compliance with local laws and for the trading decisions you make with this tool. The US, UK, and several other jurisdictions restrict or prohibit retail access to prediction markets — **check your local regulations before running this software**.

---

## 🚀 Quick Start

```bash
git clone https://github.com/yourname/polymarket-trader-toolkit.git
cd polymarket-trader-toolkit
./scripts/install.sh          # sets up Python venv + deps
cp examples/balanced_smart_money.json my-config.json
./scripts/replay.sh my-config.json --days 30
./scripts/deploy.sh my-config.json --dry-run
```

See [docs/getting-started.md](docs/getting-started.md) for the full walkthrough.

---

## ✨ What's Inside

| Module | What it does | Status |
|---|---|---|
| **Smart-Money Copy Trader** | Auto-mirror top trader positions with risk caps | ✅ Production |
| **Web Dashboard** | Real-time positions, PnL, alerts | ✅ Production |
| **Hedging Engine** | YES/NO pair arbitrage + spread monitor | ✅ Production |
| **BTC 5m Fast-Loop** | CEX momentum → Polymarket fast markets | ✅ Production (high risk) |
| **Replay Tool** | Backtest any config on historical data | ✅ Production |
| **Notifier** | Feishu / Discord / Telegram alerts | ✅ Production |

**45+ tests, 100+ commits, 17 production bugs already fixed.** See [CHANGELOG.md](CHANGELOG.md).

---

## 📦 Pricing

This is the **free, open-source core**. For the full toolkit, choose a tier:

| | **Solo Trader — $49** | **Pro Quant — $129** |
|---|---|---|
| All open-source code | ✅ | ✅ |
| 3 production config templates | ✅ | ✅ |
| Local deploy scripts | ✅ | ✅ |
| Hedging calculator UI | — | ✅ |
| PnL attribution replay | — | ✅ |
| Feishu / Telegram templates | — | ✅ |
| 1-hour onboarding call | — | ✅ |
| 6-week exclusive updates | — | ✅ |
| Discord @solo (6 mo) | ✅ | ✅ |
| Discord @pro (12 mo) | — | ✅ |
| White-label rights | — | ✅ (apply) |

**→ [Buy on Gumroad](https://yournamespace.gumroad.com/l/polymarket-toolkit)**

USDC on Polygon also accepted — see [pricing/payment.md](pricing/payment.md).

---

## 📚 Documentation

- [Getting Started](docs/getting-started.md)
- [Strategy Playbook](docs/strategy-playbook.md)
- [Safety & Disclaimers](docs/safety.md)
- [FAQ](docs/faq.md)
- [Pro tier details](pricing/payment.md)

---

## 🧪 Development

```bash
make test          # run pytest (45+ tests)
make lint          # ruff + mypy
make replay DEMO=1 # dry-run replay example
```

PRs welcome for bug fixes. New strategies go in the Pro tier (see [CONTRIBUTING.md](CONTRIBUTING.md)).

---

## 📜 License

**Business Source License 1.1** — see [LICENSE](LICENSE).

You may use, modify, and self-host this software for personal or internal business use. You may **not** resell or sublicense it. After 4 years, each release automatically converts to Apache 2.0.

For commercial redistribution rights, see the **Pro Quant** tier.

---

## 💬 Community

- Discord: [invite link in Gumroad receipt]
- Discussions: [GitHub Discussions](../../discussions)
- Issues: [GitHub Issues](../../issues) (Pro customers get priority response in Discord)

---

## 🌟 Acknowledgments

Built by traders who lost money first, then wrote the tools they wished they'd had.

Inspired by: ccxt, hummingbot, freqtrade — but for prediction markets.