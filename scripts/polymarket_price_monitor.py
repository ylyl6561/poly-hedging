"""
Polymarket Market Price Monitor

Monitors Polymarket BTC fast markets and records:
- UP (YES) and DOWN (NO) prices every second
- Actual fill prices from trades (if captured via orderbook)
- Generates a simple HTML report

Usage:
    python scripts/polymarket_price_monitor.py
    python scripts/polymarket_price_monitor.py --asset BTC --duration 300
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.constants import ASSET_PATTERNS, WINDOW_SECONDS, CLOB_API


def api_request(url, timeout=10):
    """Make an HTTP GET request. Returns parsed JSON or None on error."""
    try:
        from urllib.request import urlopen, Request
        req = Request(url, headers={"User-Agent": "poly-simmer-monitor/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"  [WARN] API request failed: {e}")
        return None


def get_current_slot(window="5m"):
    """Get current market slot timestamp for the given window."""
    seconds = WINDOW_SECONDS.get(window, 300)
    return int(datetime.now(timezone.utc).timestamp()) // seconds * seconds


def discover_current_markets(asset="BTC", window="5m"):
    """Discover current live fast market by deterministic slug for reliable price data."""
    asset = (asset or "BTC").upper()
    prefix_map = {"BTC": "btc", "ETH": "eth", "SOL": "sol"}
    prefix = prefix_map.get(asset)
    if not prefix:
        return []
    
    window_seconds = WINDOW_SECONDS.get(window, 300)
    now = datetime.now(timezone.utc)
    current_slot = get_current_slot(window)
    
    # Only check the CURRENT slot (not previous/next) to avoid duplicates
    slug = f"{prefix}-updown-{window}-{current_slot}"
    url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
    event = api_request(url)
    
    if not event or isinstance(event, dict) and event.get("error"):
        return []
    
    markets = []
    for m in event.get("markets") or []:
        if m.get("closed"):
            continue
        
        # Parse outcome prices
        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, ValueError):
                outcomes = []
        
        outcome_prices_raw = m.get("outcomePrices", "[]")
        if isinstance(outcome_prices_raw, str):
            try:
                outcome_prices = json.loads(outcome_prices_raw)
            except (json.JSONDecodeError, ValueError):
                outcome_prices = []
        else:
            outcome_prices = outcome_prices_raw or []
        
        # Parse token IDs
        clob_tokens_raw = m.get("clobTokenIds", "[]")
        if isinstance(clob_tokens_raw, str):
            try:
                clob_token_ids = json.loads(clob_tokens_raw)
            except (json.JSONDecodeError, ValueError):
                clob_token_ids = []
        else:
            clob_token_ids = clob_tokens_raw or []
        
        # Determine YES/NO token indices and prices
        yes_token = None
        no_token = None
        yes_price = None
        no_price = None
        
        for i, outcome in enumerate(outcomes):
            if i < len(outcome_prices):
                try:
                    price = float(outcome_prices[i])
                except (ValueError, TypeError):
                    price = None
            else:
                price = None
            
            outcome_upper = outcome.upper() if outcome else ""
            if "UP" in outcome_upper or "YES" in outcome_upper:
                yes_token = clob_token_ids[i] if i < len(clob_token_ids) else None
                yes_price = price
            elif "DOWN" in outcome_upper or "NO" in outcome_upper:
                no_token = clob_token_ids[i] if i < len(clob_token_ids) else None
                no_price = price
        
        end_dt = datetime.fromtimestamp(current_slot + window_seconds, tz=timezone.utc)
        remaining = (end_dt - now).total_seconds()
        is_live = (
            bool(event.get("active", True))
            and bool(m.get("active", True))
            and bool(m.get("acceptingOrders", True))
            and 0 < remaining <= window_seconds
        )
        
        markets.append({
            "question": m.get("question") or event.get("title") or "",
            "slug": m.get("slug") or slug,
            "slot": current_slot,
            "condition_id": m.get("conditionId", ""),
            "end_time": end_dt,
            "remaining_seconds": remaining,
            "is_live": is_live,
            "outcomes": outcomes,
            "outcome_prices": outcome_prices,
            "yes_token": yes_token,
            "no_token": no_token,
            "yes_price": yes_price,
            "no_price": no_price,
            "best_bid": m.get("bestBid"),
            "best_ask": m.get("bestAsk"),
            "last_trade": m.get("lastTradePrice"),
            "fee_rate_bps": m.get("fee_rate_bps") or m.get("feeRateBps") or 0,
            "spread": m.get("spread"),
            "volume": m.get("volume", 0),
        })
    
    return markets


def fetch_market_price_from_gamma(slug):
    """Fetch current market price from Gamma API by slug."""
    url = f"https://gamma-api.polymarket.com/markets/slug/{quote(slug)}"
    result = api_request(url, timeout=5)
    if not result:
        return None
    
    # Parse outcome prices
    outcome_prices_raw = result.get("outcomePrices", "[]")
    if isinstance(outcome_prices_raw, str):
        try:
            outcome_prices = json.loads(outcome_prices_raw)
        except (json.JSONDecodeError, ValueError):
            outcome_prices = []
    else:
        outcome_prices = outcome_prices_raw or []
    
    # Parse outcomes
    outcomes = result.get("outcomes", [])
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (json.JSONDecodeError, ValueError):
            outcomes = []
    
    # Determine YES/NO prices
    yes_price = None
    no_price = None
    for i, outcome in enumerate(outcomes):
        if i < len(outcome_prices):
            try:
                price = float(outcome_prices[i])
            except (ValueError, TypeError):
                price = None
        else:
            price = None
        
        outcome_upper = outcome.upper() if outcome else ""
        if "UP" in outcome_upper or "YES" in outcome_upper:
            yes_price = price
        elif "DOWN" in outcome_upper or "NO" in outcome_upper:
            no_price = price
    
    return {
        "yes_price": yes_price,
        "no_price": no_price,
        "best_bid": result.get("bestBid"),
        "best_ask": result.get("bestAsk"),
        "last_trade": result.get("lastTradePrice"),
        "spread": result.get("spread"),
        "volume": result.get("volume", 0),
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
    }


def fetch_orderbook_prices(token_id):
    """Fetch orderbook best bid/ask for a token from CLOB API.
    
    Returns:
        dict with best_bid (sell price), best_ask (buy price), or None on error.
    """
    if not token_id:
        return None
    
    result = api_request(f"{CLOB_API}/book?token_id={quote(str(token_id))}", timeout=5)
    if not result or isinstance(result, dict) and result.get("error"):
        return None
    
    bids = result.get("bids", [])
    asks = result.get("asks", [])
    
    best_bid = None
    best_ask = None
    
    try:
        if bids:
            sorted_bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
            best_bid = float(sorted_bids[0]["price"])
        
        if asks:
            sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
            best_ask = float(sorted_asks[0]["price"])
    except (KeyError, ValueError, IndexError, TypeError):
        pass
    
    return {"best_bid": best_bid, "best_ask": best_ask}


def parse_args():
    parser = argparse.ArgumentParser(description="Monitor Polymarket BTC fast market prices")
    parser.add_argument("--asset", default="BTC", help="Asset to monitor (BTC, ETH, SOL)")
    parser.add_argument("--window", default="5m", help="Window duration (5m, 15m)")
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval in seconds")
    parser.add_argument("--output-dir", default="runs/price_monitor", help="Output directory for reports")
    return parser.parse_args()


def main():
    args = parse_args()
    
    print("=" * 60)
    print("Polymarket Price Monitor")
    print("=" * 60)
    print(f"  Asset:     {args.asset}")
    print(f"  Window:   {args.window}")
    print(f"  Interval:  {args.interval}s")
    print(f"  Mode:     Continuous (Ctrl+C to stop)")
    print()
    
    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate run ID
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    csv_path = output_dir / f"prices_{run_id}.csv"
    report_path = output_dir / "live.html"  # Fixed filename for live refresh
    
    # Initialize data storage for all markets
    data = {}  # {slug: {"market": {...}, "samples": [...]}}
    
    print(f"Starting continuous monitoring...")
    print(f"Press Ctrl+C to stop\n")
    
    # Create CSV file
    header = ["timestamp", "market_slug", "remaining_sec",
              "up_buy_price", "up_sell_price",
              "down_buy_price", "down_sell_price",
              "last_trade", "spread", "volume"]
    
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        f.flush()
        
        # Infinite monitoring loop
        start_time = time.time()
        total_samples = 0
        current_market_slug = None
        
        try:
            while True:
                loop_start = time.time()
                now_utc = datetime.now(timezone.utc)
                
                # Discover current live market
                markets = discover_current_markets(args.asset, args.window)
                live_markets = [m for m in markets if m.get("is_live")]
                
                if not live_markets:
                    # No live market, wait and retry
                    time.sleep(1)
                    continue
                
                market = live_markets[0]
                slug = market["slug"]
                timestamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")
                
                # Check if we switched to a new market
                if slug != current_market_slug:
                    if current_market_slug is not None:
                        print(f"\n  [SWITCH] {current_market_slug} -> {slug}")
                    else:
                        print(f"  [START] Monitoring: {market['question'][:60]}...")
                    current_market_slug = slug
                    
                    # Initialize data for new market
                    if slug not in data:
                        data[slug] = {"market": market, "samples": []}
                        print(f"  [INFO] Samples so far: {total_samples}")
                
                # Fetch orderbook for UP (YES) token
                up_orderbook = fetch_orderbook_prices(market.get("yes_token"))
                up_buy = up_orderbook.get("best_ask") if up_orderbook else None
                up_sell = up_orderbook.get("best_bid") if up_orderbook else None
                
                # Fetch orderbook for DOWN (NO) token
                down_orderbook = fetch_orderbook_prices(market.get("no_token"))
                down_buy = down_orderbook.get("best_ask") if down_orderbook else None
                down_sell = down_orderbook.get("best_bid") if down_orderbook else None
                
                # Calculate spread
                spread = None
                if up_buy and up_sell:
                    spread = up_buy - up_sell
                
                # Get last trade and volume
                gamma_data = fetch_market_price_from_gamma(slug)
                last_trade = gamma_data.get("last_trade") if gamma_data else None
                volume = gamma_data.get("volume", 0) if gamma_data else 0
                
                # Calculate remaining time
                remaining = (market.get("end_time") - now_utc).total_seconds() if market.get("end_time") else 0
                
                # Track DOWN price statistics for this market
                down_stats = data[slug].get("down_stats", {
                    "final_down_bid": None,
                    "final_down_ask": None,
                    "reached_99": False,
                    "reached_99_and_below_45": False,
                    "reached_99_and_below_40": False,
                    "reached_99_and_below_30": False,
                    "min_down_bid": None,
                    "min_down_ask": None,
                })
                
                # Update DOWN stats
                if down_sell is not None:  # DOWN Bid
                    # Track if ever reached 0.99
                    if down_sell >= 0.99:
                        down_stats["reached_99"] = True
                    
                    # Track minimum DOWN bid
                    if down_stats["min_down_bid"] is None or down_sell < down_stats["min_down_bid"]:
                        down_stats["min_down_bid"] = down_sell
                    
                    # Check compound conditions
                    if down_stats["reached_99"]:
                        if down_sell < 0.45:
                            down_stats["reached_99_and_below_45"] = True
                        if down_sell < 0.40:
                            down_stats["reached_99_and_below_40"] = True
                        if down_sell < 0.30:
                            down_stats["reached_99_and_below_30"] = True
                    
                    # Update final value when near end
                    if remaining <= 5:
                        down_stats["final_down_bid"] = down_sell
                
                if down_buy is not None:  # DOWN Ask
                    if down_stats["min_down_ask"] is None or down_buy < down_stats["min_down_ask"]:
                        down_stats["min_down_ask"] = down_buy
                    if remaining <= 5:
                        down_stats["final_down_ask"] = down_buy
                
                data[slug]["down_stats"] = down_stats
                
                # Print stats when market is ending
                if remaining <= 5 and remaining > 0 and down_stats.get("reached_99"):
                    ds = down_stats
                    print(f"\n  [END STATS] {slug[-20:]}")
                    print(f"    结束价格: {ds.get('final_down_bid', 'N/A')}")
                    print(f"    曾达0.99: {'是' if ds.get('reached_99') else '否'}")
                    print(f"    曾<0.45: {'是' if ds.get('reached_99_and_below_45') else '否'}")
                    print(f"    曾<0.40: {'是' if ds.get('reached_99_and_below_40') else '否'}")
                    print(f"    曾<0.30: {'是' if ds.get('reached_99_and_below_30') else '否'}")
                
                # Write to CSV
                writer.writerow([
                    timestamp,
                    slug,
                    f"{remaining:.0f}",
                    f"{up_buy:.4f}" if up_buy else "",
                    f"{up_sell:.4f}" if up_sell else "",
                    f"{down_buy:.4f}" if down_buy else "",
                    f"{down_sell:.4f}" if down_sell else "",
                    f"{last_trade:.4f}" if last_trade else "",
                    f"{spread:.4f}" if spread else "",
                    str(volume) if volume else "0",
                ])
                f.flush()
                
                # Store in memory
                sample = {
                    "timestamp": timestamp,
                    "remaining": remaining,
                    "up_buy": up_buy,
                    "up_sell": up_sell,
                    "down_buy": down_buy,
                    "down_sell": down_sell,
                    "last_trade": last_trade,
                    "spread": spread,
                    "volume": volume,
                }
                data[slug]["samples"].append(sample)
                total_samples += 1
                
                # Always update the live report file
                generate_report(data, report_path, args.asset, args.window, run_id, interval=args.interval)
                
                # Sleep for interval
                elapsed = time.time() - loop_start
                sleep_time = max(0, args.interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)
                
                # Progress indicator
                elapsed_total = time.time() - start_time
                mins = int(elapsed_total / 60)
                secs = int(elapsed_total % 60)
                print(f"\r  [{mins:02d}:{secs:02d}] Samples: {total_samples} | Markets: {len(data)} | {slug[-20:]}", end="", flush=True)
                
        except KeyboardInterrupt:
            print("\n\n" + "=" * 40)
            print("Stopping monitor...")
    
    print(f"\n\nMonitoring completed.")
    print(f"  Total samples: {total_samples}")
    print(f"  Markets tracked: {len(data)}")
    print(f"  Duration: {int(time.time() - start_time)}s")
    print(f"  Data saved to: {csv_path}")
    
    # Generate HTML report with all data in one chart
    generate_report(data, report_path, args.asset, args.window, run_id, interval=args.interval)
    print(f"  Report generated: {report_path}")


def generate_report(data, report_path, asset, window, run_id, interval=1.0):
    """Generate an HTML report with a single combined chart for all markets.
    
    Args:
        data: Dict mapping slug to {"market": {...}, "samples": [...]}
        report_path: Path to write the HTML report
        asset: Asset symbol (BTC, ETH, SOL)
        window: Window duration (5m, 15m)
        run_id: Run identifier
        interval: Sampling interval in seconds
    """
    # Flatten all samples into a single timeline
    all_samples = []
    market_info = {}
    
    # Color palette for markets
    color_palette = [
        "#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#00BCD4",
        "#E91E63", "#8BC34A", "#FF5722", "#795548", "#607D8B"
    ]
    
    for idx, (slug, market_data) in enumerate(data.items()):
        m = market_data["market"]
        question = m.get("question", slug)
        color = color_palette[idx % len(color_palette)]
        market_info[slug] = {"color": color, "question": question}
        
        for sample in market_data["samples"]:
            all_samples.append({
                "timestamp": sample["timestamp"],
                "slug": slug,
                "up_buy": sample.get("up_buy"),
                "up_sell": sample.get("up_sell"),
                "down_buy": sample.get("down_buy"),
                "down_sell": sample.get("down_sell"),
            })
    
    # Sort by timestamp
    all_samples.sort(key=lambda x: x["timestamp"])
    
    # Prepare chart data arrays
    labels = [s["timestamp"][11:] for s in all_samples]
    up_buy_data = ["null" if s["up_buy"] is None else s["up_buy"] for s in all_samples]
    up_sell_data = ["null" if s["up_sell"] is None else s["up_sell"] for s in all_samples]
    down_buy_data = ["null" if s["down_buy"] is None else s["down_buy"] for s in all_samples]
    down_sell_data = ["null" if s["down_sell"] is None else s["down_sell"] for s in all_samples]
    
    total_samples = sum(len(d['samples']) for d in data.values())
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="3">  <!-- Auto-refresh every 3 seconds -->
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket 价格监控报告</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }}
        h1 {{ color: #333; }}
        h2 {{ color: #555; border-bottom: 2px solid #ddd; padding-bottom: 8px; }}
        .summary {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .chart-section {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .chart-container {{
            position: relative;
            height: 500px;
            margin: 20px 0;
        }}
        .market-legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
            margin: 15px 0;
            padding: 10px;
            background: #f8f9fa;
            border-radius: 6px;
        }}
        .market-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 5px 10px;
            border-radius: 4px;
            font-size: 0.9em;
        }}
        .market-dot {{
            width: 12px;
            height: 12px;
            border-radius: 50%;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
        }}
        .stat-box {{
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
        }}
        .stat-label {{
            color: #666;
            font-size: 0.85em;
            margin-bottom: 5px;
        }}
        .stat-value {{
            color: #222;
            font-size: 1.2em;
            font-weight: 600;
        }}
        .down-stats {{
            margin-top: 30px;
        }}
        .down-stats h3 {{
            color: #dc3545;
            border-bottom: 2px solid #dc3545;
            padding-bottom: 8px;
        }}
        .down-stats table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }}
        .down-stats th, .down-stats td {{
            padding: 10px 15px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        .down-stats th {{
            background: #f8f9fa;
            font-weight: 600;
        }}
        .down-stats .yes {{ color: #28a745; font-weight: bold; }}
        .down-stats .no {{ color: #dc3545; font-weight: bold; }}
        .footer {{
            text-align: center;
            color: #888;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
        }}
    </style>
</head>
<body>
    <h1>Polymarket 价格监控报告</h1>
    
    <div class="summary">
        <h2>概要</h2>
        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-label">交易品种</div>
                <div class="stat-value">{asset}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">窗口</div>
                <div class="stat-value">{window}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">运行ID</div>
                <div class="stat-value">{run_id}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">监控市场数</div>
                <div class="stat-value">{len(data)}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">采样总数</div>
                <div class="stat-value">{total_samples}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">采样间隔</div>
                <div class="stat-value">{interval}s</div>
            </div>
        </div>
    </div>
    
    <div class="down-stats">
        <h3>DOWN 价格统计</h3>
        <table>
            <thead>
                <tr>
                    <th>市场</th>
                    <th>结束价格 (Bid)</th>
                    <th>最低价格</th>
                    <th>DOWN达0.99?</th>
                    <th>且曾&lt;0.45?</th>
                    <th>且曾&lt;0.40?</th>
                    <th>且曾&lt;0.30?</th>
                </tr>
            </thead>
            <tbody>
"""
    
    for slug, market_data in data.items():
        down_stats = market_data.get("down_stats", {})
        final_bid = down_stats.get("final_down_bid")
        min_bid = down_stats.get("min_down_bid")
        reached_99 = down_stats.get("reached_99", False)
        below_45 = down_stats.get("reached_99_and_below_45", False)
        below_40 = down_stats.get("reached_99_and_below_40", False)
        below_30 = down_stats.get("reached_99_and_below_30", False)
        
        short_slug = slug[-20:] if len(slug) > 20 else slug
        final_str = f"{final_bid:.4f}" if final_bid else "N/A"
        min_str = f"{min_bid:.4f}" if min_bid else "N/A"
        
        yes_class_99 = "yes" if reached_99 else "no"
        yes_text_99 = "是" if reached_99 else "否"
        yes_class_45 = "yes" if below_45 else "no"
        yes_text_45 = "是" if below_45 else "否"
        yes_class_40 = "yes" if below_40 else "no"
        yes_text_40 = "是" if below_40 else "否"
        yes_class_30 = "yes" if below_30 else "no"
        yes_text_30 = "是" if below_30 else "否"
        
        html += (
            '<tr>'
            '<td><code>' + short_slug + '</code></td>'
            '<td>' + final_str + '</td>'
            '<td>' + min_str + '</td>'
            '<td class="' + yes_class_99 + '">' + yes_text_99 + '</td>'
            '<td class="' + yes_class_45 + '">' + yes_text_45 + '</td>'
            '<td class="' + yes_class_40 + '">' + yes_text_40 + '</td>'
            '<td class="' + yes_class_30 + '">' + yes_text_30 + '</td>'
            '</tr>\n'
        )
    
    html += (
        '</tbody>\n'
        '</table>\n'
        '</div>\n\n'
        '<div class="chart-section">\n'
        '<h2>价格趋势图 (所有市场)</h2>\n\n'
        '<div class="market-legend">\n'
        '<strong>市场:</strong>\n'
    )
    
    # Add market legend items
    for slug, info in market_info.items():
        short_slug = slug[-18:] if len(slug) > 18 else slug
        html += '<div class="market-item"><span class="market-dot" style="background-color: ' + info["color"] + '"></span><span>' + short_slug + '</span></div>\n'
    
    html += (
        '</div>\n\n'
        '<div class="chart-container">\n'
        '<canvas id="priceChart"></canvas>\n'
        '</div>\n'
        '</div>\n\n'
        '<script>\n'
        "Chart.defaults.font.family = \"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif\";\n\n"
        "const ctx = document.getElementById('priceChart');\n"
        "if (ctx) {\n"
        "    new Chart(ctx, {\n"
        "        type: 'line',\n"
        "        data: {\n"
        "            labels: " + str(labels) + ",\n"
        "            datasets: [\n"
        "                {\n"
        "                    label: 'UP Ask (买入)',\n"
        "                    data: " + str(up_buy_data) + ",\n"
        "                    borderColor: '#28a745',\n"
        "                    backgroundColor: 'rgba(40, 167, 69, 0.1)',\n"
        "                    borderWidth: 2,\n"
        "                    tension: 0.3,\n"
        "                    fill: false,\n"
        "                    pointRadius: 3,\n"
        "                    pointHoverRadius: 5\n"
        "                },\n"
        "                {\n"
        "                    label: 'UP Bid (卖出)',\n"
        "                    data: " + str(up_sell_data) + ",\n"
        "                    borderColor: 'rgba(40, 167, 69, 0.5)',\n"
        "                    borderWidth: 2,\n"
        "                    tension: 0.3,\n"
        "                    borderDash: [5, 5],\n"
        "                    fill: false,\n"
        "                    pointRadius: 2,\n"
        "                    pointHoverRadius: 4\n"
        "                },\n"
        "                {\n"
        "                    label: 'DOWN Ask (买入)',\n"
        "                    data: " + str(down_buy_data) + ",\n"
        "                    borderColor: '#dc3545',\n"
        "                    backgroundColor: 'rgba(220, 53, 69, 0.1)',\n"
        "                    borderWidth: 2,\n"
        "                    tension: 0.3,\n"
        "                    fill: false,\n"
        "                    pointRadius: 3,\n"
        "                    pointHoverRadius: 5\n"
        "                },\n"
        "                {\n"
        "                    label: 'DOWN Bid (卖出)',\n"
        "                    data: " + str(down_sell_data) + ",\n"
        "                    borderColor: 'rgba(220, 53, 69, 0.5)',\n"
        "                    borderWidth: 2,\n"
        "                    tension: 0.3,\n"
        "                    borderDash: [5, 5],\n"
        "                    fill: false,\n"
        "                    pointRadius: 2,\n"
        "                    pointHoverRadius: 4\n"
        "                }\n"
        "            ]\n"
        "        },\n"
        "        options: {\n"
        "            responsive: true,\n"
        "            maintainAspectRatio: false,\n"
        "            interaction: {\n"
        "                mode: 'index',\n"
        "                intersect: false\n"
        "            },\n"
        "            plugins: {\n"
        "                legend: {\n"
        "                    position: 'top',\n"
        "                    labels: {\n"
        "                        usePointStyle: true,\n"
        "                        padding: 15\n"
        "                    }\n"
        "                },\n"
        "                tooltip: {\n"
        "                    backgroundColor: 'rgba(0, 0, 0, 0.8)',\n"
        "                    padding: 12\n"
        "                }\n"
        "            },\n"
        "            scales: {\n"
        "                x: {\n"
        "                    title: {\n"
        "                        display: true,\n"
        "                        text: '时间 (UTC)'\n"
        "                    },\n"
        "                    ticks: {\n"
        "                        maxRotation: 45,\n"
        "                        minRotation: 45,\n"
        "                        maxTicksLimit: 30\n"
        "                    }\n"
        "                },\n"
        "                y: {\n"
        "                    title: {\n"
        "                        display: true,\n"
        "                        text: '价格'\n"
        "                    },\n"
        "                    min: 0,\n"
        "                    max: 1,\n"
        "                    ticks: {\n"
        "                        callback: function(value) {\n"
        "                            return value.toFixed(2);\n"
        "                        }\n"
        "                    }\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    });\n"
        "}\n"
        "</script>\n\n"
        '<div class="footer">\n'
        "<p>由 poly-simmer-fast-loop 价格监控生成</p>\n"
        "<p>运行ID: " + run_id + "</p>\n"
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )
    
    with open(report_path, "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
