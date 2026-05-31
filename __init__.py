"""
Poly-simmer-fast-loop: Polymarket BTC fast-market trading system.

This package is organized as follows:
- core/       : Constants and configuration
- signals/    : CEX momentum signals and fair-value model
- trading/    : Trade execution layer
- market/     : Market discovery and RTDS price feeds
- state/      : State management and structured logging
- api/        : HTTP client and Polymarket CLOB API
- notifications/: Feishu/Lark notifications
- main/       : CLI entry point and strategy orchestration
- replay/     : Candidate journal replay tool
- signal_evaluators/: YES/NO/Hedge signal evaluators
"""

__version__ = "0.1.0"
