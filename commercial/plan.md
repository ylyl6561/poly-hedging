# poly-hedging — 商业化 MVP 方案

> **TL;DR**：把 `poly-hedging` 仓库（Polymarket 事件对冲工具集，3 个策略）拆分成 1 个开源仓库 + 1 个商业仓库（模板 + 文档 + 支持），**$99 单品**，首批 100 用户 4 周内可达。

> **项目背景 / 是什么**：见 [`overview.md`](overview.md) —— 简单说就是 Polymarket 上的事件对冲工具集：**BTC 5 分钟 FastLoop**（CEX 动量交易快闪市场）+ **Top 用户跟单**（复制排行榜钱包）+ **双钱包事件对冲**（YES/NO 配对套利）。这三者共用同一套 CLOB / 数据 / 通知层，所以打包卖。

---

## 1. 产品定位（这是最重要的决策）

### ❌ 不要这样定位
- "Polymarket 信号订阅" / "包赢跟单机器人" / "稳赚策略"
- 任何隐含"保证盈利"或"金融建议"的措辞
- 美区散户可一键订阅自动下注的 SaaS（CFTC / 各州监管雷区）

### ✅ 这样定位
**Polymarket Trader Toolkit — 开源交易基础设施 + 商业模板包**

> "Run your own Polymarket trading desk. We open-source the engine and sell the playbook, templates, and on-call support."

价值主张（一句话版本）：
- 节省 200+ 小时自己造轮子
- 避免 17 个我们踩过的 bug（看 commit history）
- 拿到 3 个生产环境验证过的策略模板
- 包含 replay 工具，跑历史数据再决定是否上线

---

## 2. 产品目录（基于现有仓库，诚实标注）

| 模块 | 仓库位置 | 状态 | 商业化角色 |
|---|---|---|---|
| **Smart-Money 跟单** | `smart_money/` | 生产可用 | 核心卖点 #1 |
| **Dashboard** | `smart_money/dashboard/` | 生产可用 | 核心卖点 #2（可视化） |
| **Hedging 对冲工具** | `hedging/` | 生产可用 | 差异化卖点 #3 |
| **BTC 5m Fast-Loop** | `strategy/` + `main/fastloop_trader.py` | 生产可用 | 高级策略（高风险） |
| **Replay 分析工具** | `scripts/` + `runs/` | 生产可用 | 卖点 #4（量化友好） |
| **飞书 / 通知** | `notifications/` | 生产可用 | 卖点 #5（运营友好） |
| **Scheduler 调度** | `scheduler/` | 生产可用 | 卖点 #6（自动化） |
| **Tests + CI** | `tests/` | 45+ 测试 | 卖点 #7（质量保证） |

> ⚠️ **不能承诺的事**（要在所有地方明示）：
> - 不保证盈利 / ROI
> - 不是金融建议
> - 美区用户在 Polymarket 直接交易受限，工具本身不受限
> - 自动跟单功能由用户决定如何使用，开发者不承担盈亏责任

---

## 3. 单品定价：**$99**

目标：自己跑 1-3 个策略的散户 / quant / 团队

**Polymarket Trader Toolkit — $99**（一次买断）

包含：
- ✅ 完整开源核心代码（永远免费，会持续更新）
- ✅ **3 个生产环境验证过的配置模板**：
  - `conservative_btc_5m.json` — 低风险 BTC 5m
  - `balanced_smart_money.json` — 跟单 + 对冲
  - `aggressive_pairs_hedge.json` — YES/NO 配对套利
- ✅ **5 个 .env 模板**（testnet / mainnet 各场景）
- ✅ **本地部署脚本**（一键 `make deploy`）
- ✅ **Hedging 计算器 Web UI**
- ✅ **Replay 分析 Pro 脚本**（含 PnL 归因、Sharpe、最大回撤）
- ✅ **飞书 / Telegram 通知模板**（5 种告警规则）
- ✅ **6 周独家更新窗口**（新策略、新交易所适配）
- ✅ **1 小时一对一 onboarding**（Zoom，30 天内预约）
- ✅ **Discord 频道访问**（12 个月）
- ✅ **白标授权**（可卖给客户，不限数量，需书面申请）
- ✅ **Lifetime updates**（未来所有版本免费）

不含：完全定制的策略开发（按 $200/小时另算）、托管交易。

### Upsell
- **Lifetime pass $399**：一次买断，未来所有 Pro-tier 功能升级
- **Team pack $499**：5 个席位共享 onboarding（同一公司内部用）

---

## 4. GitHub 仓库结构

### 仓库 A：`polymarket-trader-toolkit`（开源，BSL 1.1 → 实际可商用但限制再分发）
```
polymarket-trader-toolkit/
├── README.md                  # 主入口，含定价 + 跳转购买链接
├── LICENSE                    # Business Source License 1.1
├── NOTICE                     # 商业授权说明
├── docs/
│   ├── getting-started.md
│   ├── strategy-playbook.md
│   ├── safety.md              # 重要：风险、监管、自担责任
│   └── faq.md
├── examples/                  # 3 个模板配置
│   ├── conservative_btc_5m.json
│   ├── balanced_smart_money.json
│   └── aggressive_pairs_hedge.json
├── scripts/
│   ├── install.sh
│   ├── deploy.sh
│   └── replay.sh
├── tools/                     # 命令行 replay / 报告生成
│   └── replay_cli.py
├── .github/
│   ├── workflows/ci.yml       # pytest + lint
│   ├── ISSUE_TEMPLATE/
│   └── DISCUSSIONS/
└── CHANGELOG.md
```

### 仓库 B：`polymarket-trader-toolkit-pro`（闭源，付费后通过 Gumroad / Lemon Squeezy 下载）
```
polymarket-trader-toolkit-pro/
├── README.md
├── LICENSE                    # Commercial, all rights reserved
├── src/
│   ├── hedging_calculator/    # Web UI
│   ├── replay_pro/            # PnL 归因
│   └── notifiers/             # 飞书/电报模板
├── templates/
│   ├── feishu_alerts.yaml     # 5 种告警
│   ├── telegram_bot.py
│   └── discord_webhook.py
├── docs/
│   ├── onboarding.md
│   └── api_reference.md
└── SUPPORT.md
```

**为何 BSL 而非 MIT/Apache？**
- 允许个人/小公司免费使用
- 禁止竞争对手 / 大公司直接 fork 转售
- 4 年后自动转 Apache 2.0（社区友好）
- 标准做法，HashiCorp / CockroachDB / Sentry 都在用

---

## 5. Landing Page（详见 `landing/index.html`）

单文件 HTML，无构建步骤，可直接拖到 Vercel / Netlify / Cloudflare Pages / S3。

结构：
1. **Hero** — "Hedge your exposure on Polymarket. Open-source engine, paid toolkit."
2. **Demo GIF** — Dashboard 截图（占位）
3. **6 卡片** — 跟单 / 对冲 / BTC 5m / Dashboard / Replay / Notifier 六大功能
4. **Pricing** — 单一 $99 套餐（All-In）
5. **Trust signals** — "45+ tests, 100+ commits, 17 bugs we've already fixed"
6. **FAQ** — 7 个高频问题（含"能赚钱吗？"的合规回答）
7. **CTA** — "Buy the Toolkit — $99"（带 FIRST48 20% off 折扣码）

设计原则：和现有 dashboard 同款暗色主题，monospace + accent 颜色 = 统一品牌。

---

## 6. 支付 + 交付（详见 `pricing/payment.md`）

| 平台 | 用法 | 优势 | 劣势 |
|---|---|---|---|
| **Creem** | **主推**，含税 / VAT 自动处理 + 支付宝 + USDC 双通道 | 抽成最低 3.9% + $0.40、内置 license key、大陆个人可用 | 平台较新、生态不如 Stripe |
| **Lemon Squeezy** | 备选（EU MoR） | 税务 + 合规更友好 | UI 老旧、抽 5%+ |
| **USDC on Polygon** | 加分项，crypto 原生用户 | 0 抽成（自托管）、匿名、24/7 | 需自己生成 license key + 验证 webhook |
| ~~Gumroad~~ | 已弃用 | — | 大陆个人无 Stripe / PayPal，无法 payout |

**交付内容**（Creem 形式）：
- 单文件 `.zip` 含所有文档 + 模板 + 脚本
- 含 `LICENSE.txt` 含个人 license key（HMAC-SHA256 签名，Creem 自动生成）
- Discord 邀请链接（不同频道 = 不同 role）
- 安装指南 + FAQ PDF

**License key 验证**（轻量版）：
```python
# 验证 license 不需要联网，防破解即可
import hmac, hashlib
def verify(key, secret="YOUR_HMAC_SECRET"):
    return hmac.compare_digest(key.split(".")[0], hashlib.sha256(secret.encode()).hexdigest()[:32])
```

---

## 7. 首批 100 用户获客计划（详见 `growth/100-users.md`）

4 周 / 3 渠道 / 真实可达：

| 周次 | 渠道 | 目标动作 | 预期用户 |
|---|---|---|---|
| W1 | X / Twitter | 5 条 thread + 1 次热门推文评论区互动 | 30 |
| W1 | Reddit | 3 个 sub 各 1 个 long-form post | 25 |
| W2 | HN / Show HN | "Show HN: Open-source Polymarket trader toolkit" | 20 |
| W2-4 | Discord / X DM | 转化评论区互动 | 25 |
| **合计** | | | **100** |

**不依赖**：
- ❌ 付费广告（ROI 太低，niche 产品）
- ❌ KOL 投放（成本 > 单价 LTV）
- ❌ SEO（短期内不可能排到 "polymarket trading" 前）

**关键依赖**：
- ✅ 在 Polymarket Discord 发工具 demo（管理员允许的前提下）
- ✅ GitHub trending（开 PR 给上游 polymarket / py-clob-client）
- ✅ HackerNews 首页 6 小时（标题决定 80%）

---

## 8. 风险与缓解（这是商业化的核心）

| 风险 | 缓解 |
|---|---|
| **Polymarket 政策变化** | 工具不绑定单一交易所，模块化设计 |
| **监管（美区）** | 明确"toolkit 不是 signal"，含免责声明，不提供自动下注托管服务 |
| **有人 fork 后转售** | BSL 1.1 + DMCA + 监控 GitHub |
| **支持成本失控** | Discord 频道公共回答 + 文档先行；onboarding 仅 Pro |
| **退款潮** | 14 天退款；Telegram / Discord 私聊化解 |
| **GitHub repo 被举报** | LICENSE 合规、代码原创（引用注明）、不碰爬虫绕过 ToS |

---

## 9. 立即可做的下一步（按优先级）

1. ✅ **本周**：把 `poly-hedging` 核心代码 push 到 GitHub 公开仓库 `polymarket-trader-toolkit`，加 LICENSE + README
2. ✅ **本周**：Creem 创建产品页面（draft 状态），写 sales copy
3. ✅ **本周**：landing page 部署到 `polymarket-trader-toolkit.com`（Cloudflare Pages，免费）
4. ✅ **下周**：Reddit 软启动（不发广告，用工具本身的教程帖）
5. ✅ **第 3 周**：Show HN 上线
6. ✅ **第 4 周**：根据转化数据调整文案 / 定价

---

## 10. 关键 KPI（前 4 周）

- GitHub stars: 100+
- Landing page 访客: 500+
- Creem 转化率: 3%+
- 付费用户: 15+ (W4)
- Discord 活跃: 30+

**5 个月后预期：100 用户，$5,000-15,000 收入**（取决于 LTV 和续费）

---

**这不是"赚快钱"项目**，但是 **niche + 高 LTV + 可持续** 的产品形态。Polymarket trader 是高 ARPU 细分（个人愿意付 $100+ 换时间），且竞争远小于通用量化框架（ccxt / freqtrade 圈）。