# Contributing

Thanks for your interest in the Polymarket Trader Toolkit!

## What we accept

✅ **Bug fixes** — always welcome, please open a PR with a test
✅ **Documentation improvements** — typo fixes, clarifications, examples
✅ **Exchange API adapters** — for new prediction markets (Kalshi, Limitless, etc.)
✅ **Test coverage** — for uncovered edge cases

## What goes in Pro tier only

🔒 **New strategy templates** — to maintain the commercial value
🔒 **Replay Pro features** — PnL attribution, Sharpe calculator
🔒 **Hedging calculator UI** — Web frontend
🔒 **Custom notifier templates** — Feishu/Telegram integrations

If you're unsure, open a Discussion first.

## Development setup

```bash
git clone https://github.com/yourname/polymarket-trader-toolkit.git
cd polymarket-trader-toolkit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install
```

## Pull request process

1. Fork the repo and create a feature branch (`git checkout -b fix/order-rejection-bug`)
2. Add tests for new behavior
3. Run `make test lint` — all must pass
4. Reference any related issue
5. Sign your commits (`git commit -s`)

## Code style

- Python: `ruff format` + `ruff check` + `mypy`
- Type hints required on all public functions
- Docstrings: Google style
- Commit messages: imperative mood, ≤72 chars subject, body explains "why"

## Security issues

**DO NOT** open a public issue for security vulnerabilities. Email yourname@example.com with `[SECURITY]` in the subject.

## Code of conduct

Be kind. We're all here to learn and build. No harassment, no spam, no shilling.

## License

By contributing, you agree your contributions are licensed under the same Business Source License 1.1 as the project.