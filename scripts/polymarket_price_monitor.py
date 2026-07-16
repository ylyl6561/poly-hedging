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
from datetime import datetime, timezone, timedelta
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


def _hourly_slug_for_now(asset="BTC") -> str:
    """Compute the human-readable slug for the *current* 1h event.

    1h events are NOT keyed by epoch slot like 5m/15m — they use a
    date-based slug like ``bitcoin-up-or-down-july-12-12pm-et``.

    Args:
        asset: Asset symbol (BTC, ETH, SOL). Only BTC is currently emitted
            by Polymarket as a 1h "Up or Down" series — others fall back to
            ``coin-up-or-down-...`` which is what this helper produces.

    Returns:
        The slug pointing at the event that started at the most recent
        top-of-hour ET boundary.
    """
    asset_lower = (asset or "BTC").lower()
    # Map short asset symbol to the prefix used in slug titles.
    # Polymarket emits both "bitcoin" and "btc" forms historically;
    # observed live slug uses the full name "bitcoin".
    name_map = {"btc": "bitcoin", "eth": "ethereum", "sol": "solana"}
    coin = name_map.get(asset_lower, asset_lower)

    # Convert UTC -> ET (EDT = UTC-4 in summer, EST = UTC-5 in winter).
    # The eventStartTime in Gamma is in UTC; ET offset only affects the
    # *display label* of the slug, not when the market is live.
    now_utc = datetime.now(timezone.utc)
    # Approximate ET offset using the actual offset on today's date.
    # Python's zoneinfo avoids hard-coding EDT/EST.
    try:
        from zoneinfo import ZoneInfo
        now_et = now_utc.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: EDT (UTC-4) for summer months — covers May–Oct.
        offset_hours = -4 if 3 <= now_utc.month <= 10 else -5
        now_et = now_utc.replace(tzinfo=timezone.utc) + timedelta(hours=offset_hours)
        now_et = now_et.replace(tzinfo=None)  # strip tz for strftime

    # Top-of-hour in ET.
    hour_et = now_et.replace(minute=0, second=0, microsecond=0)
    month_lower = hour_et.strftime("%B").lower()  # e.g. "july"
    day = hour_et.day
    year = hour_et.year
    # 12-hour clock without leading zero on the hour; "12" stays as "12".
    hour12 = hour_et.strftime("%I").lstrip("0") or "12"
    ampm = hour_et.strftime("%p").lower()  # "am" or "pm"

    # IMPORTANT: the slug carries a 4-digit year segment to disambiguate
    # from prior-year events of the same month/day/hour. The series endpoint
    # confirms live events look like:
    #   bitcoin-up-or-down-july-12-2026-12pm-et
    return f"{coin}-up-or-down-{month_lower}-{day}-{year}-{hour12}{ampm}-et"


def discover_current_markets(asset="BTC", window="5m"):
    """Discover current live fast market.

    For 5m / 15m windows we use the deterministic epoch-based slug
    ``<asset>-updown-<window>-<slot>`` (e.g. ``btc-updown-5m-1783874100``).

    For the 1h window we use the human-readable slug
    ``<coin>-up-or-down-<month>-<day>-<hour><am/pm>-et``
    (e.g. ``bitcoin-up-or-down-july-12-12pm-et``), which is how
    Polymarket's hourly BTC up-or-down series names its events.
    """
    asset = (asset or "BTC").upper()
    prefix_map = {"BTC": "btc", "ETH": "eth", "SOL": "sol"}
    prefix = prefix_map.get(asset)
    if not prefix:
        return []

    window_seconds = WINDOW_SECONDS.get(window, 300)
    now = datetime.now(timezone.utc)

    if window == "1h":
        # Hourly events are not slot-aligned to the epoch; the slug carries
        # the ET wall-clock hour. Use the current top-of-hour.
        slug = _hourly_slug_for_now(asset)
        url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        # Use the ET top-of-hour UTC timestamp as the "slot" so end_dt math
        # below produces a clean 1h window boundary.
        try:
            from zoneinfo import ZoneInfo
            now_et_naive = now.astimezone(ZoneInfo("America/New_York")).replace(
                minute=0, second=0, microsecond=0
            )
            current_slot = int(now_et_naive.timestamp())
        except Exception:
            current_slot = int(now.timestamp())  # best-effort fallback
    else:
        current_slot = get_current_slot(window)
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
    parser.add_argument(
        "--windows",
        default="5m,15m,1h",
        help="Comma-separated list of windows to monitor (e.g. '5m,15m,1h'). "
             "Each window gets its own CSV and its own chart in the report.",
    )
    parser.add_argument(
        "--window",
        default=None,
        help="[Deprecated] Single-window shorthand. Overrides --windows if set.",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval in seconds")
    parser.add_argument("--browser-refresh", type=int, default=1800,
                        help="Browser auto-reload interval in seconds (default 1800 = 30 min)")
    parser.add_argument("--output-dir", default="runs/price_monitor", help="Output directory for reports")
    return parser.parse_args()


def resolve_windows(args) -> list[str]:
    """Resolve the active window list from CLI args.

    Accepts both ``--windows 5m,15m,1h`` (preferred) and legacy ``--window 5m``.
    Filters invalid entries against ``WINDOW_SECONDS``.
    """
    raw = args.window if args.window else args.windows
    parts = [w.strip() for w in raw.split(",") if w.strip()]
    valid = [w for w in parts if w in WINDOW_SECONDS]
    if not valid:
        raise SystemExit(
            f"[ERROR] No valid window provided. Got {parts!r}; "
            f"supported: {sorted(WINDOW_SECONDS.keys())}"
        )
    return valid


def main():
    args = parse_args()

    windows = resolve_windows(args)

    print("=" * 60)
    print("Polymarket Price Monitor")
    print("=" * 60)
    print(f"  Asset:     {args.asset}")
    print(f"  Windows:   {', '.join(windows)}")
    print(f"  Interval:  {args.interval}s")
    print(f"  Mode:      Continuous (Ctrl+C to stop)")
    print()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = output_dir / "live.html"

    # Per-window state, keyed by window label (e.g. "5m", "15m", "1h").
    data_by_window: dict[str, dict] = {w: {} for w in windows}

    # ------------------------------------------------------------------
    # Load all historical samples from previous runs so they appear in
    # charts immediately and new samples append to a single persistent CSV.
    # ------------------------------------------------------------------
    def _load_historical_samples(window: str) -> dict:
        """
        Load all historical CSV files for *window* from output_dir,
        merge them by slug (deduplicated by timestamp), and rebuild
        per-slug down_stats from the loaded rows.

        Returns:
            Dict: ``{slug: {"market": market_placeholder, "samples": [...],
                            "down_stats": {...}}}``
        """
        import glob, csv as _csv
        window_data = {}
        # Find every CSV that belongs to this window, regardless of run_id.
        pattern = str(output_dir / f"prices_*_{window}.csv")
        # Also match the new single file format (prices_5m.csv, etc.)
        single  = str(output_dir / f"prices_{window}.csv")
        csv_files = sorted(set(glob.glob(pattern)) | {p for p in [single] if os.path.exists(p)})
        print(f"  [{window}] scanning {len(csv_files)} historical CSV(s)...")
        seen = {}  # slug -> {timestamp -> row_idx} for deduplication

        for csv_path_str in csv_files:
            csv_path = Path(csv_path_str)
            try:
                with open(csv_path, newline="", encoding="utf-8") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        slug = row.get("market_slug", "").strip()
                        ts   = row.get("timestamp", "").strip()
                        if not slug or not ts:
                            continue
                        # Deduplicate: keep only the first occurrence of each (slug, ts).
                        if slug not in seen:
                            seen[slug] = set()
                        if ts in seen[slug]:
                            continue
                        seen[slug].add(ts)

                        # Lazily initialise this slug's slot.
                        if slug not in window_data:
                            window_data[slug] = {
                                "market": {
                                    "question": slug,
                                    "slug": slug,
                                    "condition_id": "",
                                    "end_time": None,
                                    "yes_token": "",
                                    "no_token": "",
                                },
                                "samples": [],
                                "down_stats": {
                                    "final_down_bid": None,
                                    "final_down_ask": None,
                                    "reached_99": False,
                                    "reached_99_and_below_45": False,
                                    "reached_99_and_below_40": False,
                                    "reached_99_and_below_30": False,
                                    "min_down_bid": None,
                                    "min_down_ask": None,
                                },
                            }

                        sample = {
                            "timestamp": ts,
                            "remaining": float(row["remaining_sec"]) if row.get("remaining_sec", "").strip() else 0.0,
                            "up_buy":  float(row["up_buy_price"]) if row.get("up_buy_price", "").strip() else None,
                            "up_sell": float(row["up_sell_price"]) if row.get("up_sell_price", "").strip() else None,
                            "down_buy": float(row["down_buy_price"]) if row.get("down_buy_price", "").strip() else None,
                            "down_sell": float(row["down_sell_price"]) if row.get("down_sell_price", "").strip() else None,
                            "last_trade": float(row["last_trade"]) if row.get("last_trade", "").strip() else None,
                            "spread": float(row["spread"]) if row.get("spread", "").strip() else None,
                            "volume": float(row["volume"]) if row.get("volume", "").strip() else 0.0,
                        }
                        window_data[slug]["samples"].append(sample)

                        # Rebuild down_stats incrementally — scan the full row again.
                        ds = window_data[slug]["down_stats"]
                        down_s = sample.get("down_sell")
                        down_b = sample.get("down_buy")
                        if down_s is not None:
                            if down_s >= 0.99:
                                ds["reached_99"] = True
                            if ds["min_down_bid"] is None or down_s < ds["min_down_bid"]:
                                ds["min_down_bid"] = down_s
                            if ds["reached_99"] and down_s < 0.45:
                                ds["reached_99_and_below_45"] = True
                            if ds["reached_99"] and down_s < 0.40:
                                ds["reached_99_and_below_40"] = True
                            if ds["reached_99"] and down_s < 0.30:
                                ds["reached_99_and_below_30"] = True
                        if down_b is not None:
                            if ds["min_down_ask"] is None or down_b < ds["min_down_ask"]:
                                ds["min_down_ask"] = down_b

            except Exception as e:
                print(f"  [{window}] warning: could not read {csv_path.name}: {e}")

        total = sum(len(v["samples"]) for v in window_data.values())
        print(f"  [{window}] loaded {total} historical samples across {len(window_data)} market(s)")
        return window_data

    print(f"Starting continuous monitoring across {len(windows)} window(s)...")
    print(f"Press Ctrl+C to stop\n")

    import threading

    threads = []

    def _sample_window(window: str):
        """Run a single window's sampling loop until KeyboardInterrupt.

        Samples live Polymarket markets and writes every row to a
        single persistent CSV (``prices_<window>.csv``).  On startup the
        CSV is scanned and all historical rows are pre-loaded into
        ``data_by_window[window]`` so that the HTML report shows the full
        history from the very first refresh.

        The CSV is kept open for the lifetime of the process and rows are
        appended (never overwritten) so restarts never lose data.
        """
        csv_path = output_dir / f"prices_{window}.csv"
        header = [
            "timestamp", "market_slug", "remaining_sec",
            "up_buy_price", "up_sell_price",
            "down_buy_price", "down_sell_price",
            "last_trade", "spread", "volume",
        ]

        # Pre-populate data_by_window with historical data so charts are
        # not blank after a restart.
        data_by_window[window] = _load_historical_samples(window)

        # Open CSV in append mode so previous rows are never erased.
        csv_f = open(csv_path, "a", newline="")
        writer = csv.writer(csv_f)
        # Only write the header if the file is empty (brand-new file).
        if csv_f.tell() == 0:
            writer.writerow(header)
        csv_f.flush()

        current_market_slug = None
        samples_in_window = sum(len(v["samples"]) for v in data_by_window[window].values())
        thread_start = time.time()

        # Track which slugs we've already written to the CSV (avoids
        # duplicate rows if we restart mid-session after a previous crash).
        slugs_written_to_csv = set(data_by_window[window].keys())

        print(f"\n  [{window}] sampling loop started → {csv_path}  (total rows pre-loaded: {samples_in_window})")

        try:
            while True:
                loop_start = time.time()
                now_utc = datetime.now(timezone.utc)

                markets = discover_current_markets(args.asset, window)
                live_markets = [m for m in markets if m.get("is_live")]
                if not live_markets:
                    time.sleep(1)
                    continue

                market = live_markets[0]
                slug = market["slug"]
                timestamp = now_utc.strftime("%Y-%m-%d %H:%M:%S")

                if slug != current_market_slug:
                    if current_market_slug is not None:
                        print(f"\n  [{window}] [SWITCH] {current_market_slug} -> {slug}")
                    else:
                        print(f"  [{window}] [START] {market['question'][:60]}...")
                    current_market_slug = slug
                    if slug not in data_by_window[window]:
                        data_by_window[window][slug] = {
                            "market": market,
                            "samples": [],
                            "down_stats": {
                                "final_down_bid": None,
                                "final_down_ask": None,
                                "reached_99": False,
                                "reached_99_and_below_45": False,
                                "reached_99_and_below_40": False,
                                "reached_99_and_below_30": False,
                                "min_down_bid": None,
                                "min_down_ask": None,
                            },
                        }
                    if slug not in slugs_written_to_csv:
                        slugs_written_to_csv.add(slug)

                up_orderbook = fetch_orderbook_prices(market.get("yes_token"))
                up_buy = up_orderbook.get("best_ask") if up_orderbook else None
                up_sell = up_orderbook.get("best_bid") if up_orderbook else None

                down_orderbook = fetch_orderbook_prices(market.get("no_token"))
                down_buy = down_orderbook.get("best_ask") if down_orderbook else None
                down_sell = down_orderbook.get("best_bid") if down_orderbook else None

                spread = (up_buy - up_sell) if (up_buy is not None and up_sell is not None) else None

                gamma_data = fetch_market_price_from_gamma(slug)
                last_trade = gamma_data.get("last_trade") if gamma_data else None
                volume = gamma_data.get("volume", 0) if gamma_data else 0

                remaining = (
                    (market.get("end_time") - now_utc).total_seconds()
                    if market.get("end_time") else 0
                )

                ds = data_by_window[window][slug]["down_stats"]
                if down_sell is not None:
                    if down_sell >= 0.99:
                        ds["reached_99"] = True
                    if ds["min_down_bid"] is None or down_sell < ds["min_down_bid"]:
                        ds["min_down_bid"] = down_sell
                    if ds["reached_99"]:
                        if down_sell < 0.45:
                            ds["reached_99_and_below_45"] = True
                        if down_sell < 0.40:
                            ds["reached_99_and_below_40"] = True
                        if down_sell < 0.30:
                            ds["reached_99_and_below_30"] = True
                    if remaining <= 5:
                        ds["final_down_bid"] = down_sell
                if down_buy is not None:
                    if ds["min_down_ask"] is None or down_buy < ds["min_down_ask"]:
                        ds["min_down_ask"] = down_buy
                    if remaining <= 5:
                        ds["final_down_ask"] = down_buy

                writer.writerow([
                    timestamp, slug, f"{remaining:.0f}",
                    f"{up_buy:.4f}" if up_buy else "",
                    f"{up_sell:.4f}" if up_sell else "",
                    f"{down_buy:.4f}" if down_buy else "",
                    f"{down_sell:.4f}" if down_sell else "",
                    f"{last_trade:.4f}" if last_trade else "",
                    f"{spread:.4f}" if spread else "",
                    str(volume) if volume else "0",
                ])
                csv_f.flush()

                data_by_window[window][slug]["samples"].append({
                    "timestamp": timestamp,
                    "remaining": remaining,
                    "up_buy": up_buy,
                    "up_sell": up_sell,
                    "down_buy": down_buy,
                    "down_sell": down_sell,
                    "last_trade": last_trade,
                    "spread": spread,
                    "volume": volume,
                })
                samples_in_window += 1

                elapsed = time.time() - loop_start
                sleep_time = max(0, args.interval - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

                elapsed_total = time.time() - thread_start
                mins = int(elapsed_total / 60)
                secs = int(elapsed_total % 60)
                print(
                    f"\r  [{window} {mins:02d}:{secs:02d}] samples={samples_in_window} "
                    f"markets={len(data_by_window[window])} {slug[-20:]}",
                    end="", flush=True,
                )
        except KeyboardInterrupt:
            print(f"\n  [{window}] sampling loop interrupted")
        except Exception as e:
            print(f"\n  [{window}] FATAL: {e}")
            import traceback
            traceback.print_exc()
        finally:
            try:
                csv_f.close()
            except Exception:
                pass

    for w in windows:
        t = threading.Thread(target=_sample_window, args=(w,), daemon=True)
        t.start()
        threads.append(t)

    try:
        start_time = time.time()
        last_print = 0.0
        while any(t.is_alive() for t in threads):
            time.sleep(args.interval)
            generate_report(
                data_by_window, report_path, args.asset, windows,
                run_id, interval=args.interval,
                browser_refresh_seconds=args.browser_refresh,
            )
            now = time.time()
            if now - last_print > 10:
                last_print = now
                total = sum(
                    len(sd.get("samples", []))
                    for wd in data_by_window.values()
                    for sd in wd.values()
                )
                elapsed_total = now - start_time
                mins = int(elapsed_total / 60)
                secs = int(elapsed_total % 60)
                print(
                    f"\r  [report {mins:02d}:{secs:02d}] live.html refreshed | "
                    f"total samples across windows: {total}  ",
                    end="", flush=True,
                )
    except KeyboardInterrupt:
        print("\n\nStopping monitor (joining sampling threads)...")
        for t in threads:
            t.join(timeout=2.0)

    total_samples = sum(
        len(sd.get("samples", []))
        for wd in data_by_window.values()
        for sd in wd.values()
    )
    total_markets = sum(len(wd) for wd in data_by_window.values())

    print(f"\n\nMonitoring completed.")
    print(f"  Total samples: {total_samples}")
    print(f"  Total markets tracked: {total_markets}")
    for w in windows:
        wd = data_by_window.get(w, {})
        w_samples = sum(len(sd.get("samples", [])) for sd in wd.values())
        print(f"  - {w}: {w_samples} samples across {len(wd)} market(s)")

    generate_report(
        data_by_window, report_path, args.asset, windows,
        run_id, interval=args.interval,
        browser_refresh_seconds=args.browser_refresh,
    )
    print(f"  Final report: {report_path}")


def generate_report(data_by_window, report_path, asset, windows, run_id, interval=1.0,
                     browser_refresh_seconds=1800):
    """Generate an HTML report with **one chart per window**.

    Args:
        data_by_window: ``{window_label: {slug: {"market": {...}, "samples": [...], "down_stats": {...}}}}``
        report_path: Path to write the HTML report.
        asset: Asset symbol (BTC, ETH, SOL).
        windows: List of window labels (e.g. ``["5m", "15m", "1h"]``).
        run_id: Run identifier.
        interval: Sampling interval in seconds (for display).
        browser_refresh_seconds: How often the browser should auto-reload
            the page in seconds (default 1800 = 30 min).
    """
    # Color palette for markets (one per active slug per window)
    color_palette = [
        "#2196F3", "#4CAF50", "#FF9800", "#9C27B0", "#00BCD4",
        "#E91E63", "#8BC34A", "#FF5722", "#795548", "#607D8B",
    ]

    chart_blocks = []   # (window, canvas_id, labels_js, up_buy_js, up_sell_js, down_buy_js, down_sell_js, total_samples, market_count)
    down_stats_rows = []  # rows for the per-market down-stats table

    for window in windows:
        window_data = data_by_window.get(window, {})
        all_samples = []
        market_info = {}

        for idx, (slug, market_data) in enumerate(window_data.items()):
            m = market_data["market"]
            color = color_palette[idx % len(color_palette)]
            market_info[slug] = {
                "color": color,
                "question": m.get("question", slug),
            }
            for s in market_data["samples"]:
                all_samples.append({
                    "timestamp": s["timestamp"],
                    "slug": slug,
                    "up_buy": s.get("up_buy"),
                    "up_sell": s.get("up_sell"),
                    "down_buy": s.get("down_buy"),
                    "down_sell": s.get("down_sell"),
                })
            ds = market_data.get("down_stats", {})
            down_stats_rows.append({
                "window": window,
                "slug": slug,
                "stats": ds,
            })

        all_samples.sort(key=lambda x: x["timestamp"])
        labels = [s["timestamp"][11:] for s in all_samples]
        up_buy_data = ["null" if s["up_buy"] is None else s["up_buy"] for s in all_samples]
        up_sell_data = ["null" if s["up_sell"] is None else s["up_sell"] for s in all_samples]
        down_buy_data = ["null" if s["down_buy"] is None else s["down_buy"] for s in all_samples]
        down_sell_data = ["null" if s["down_sell"] is None else s["down_sell"] for s in all_samples]
        total_samples = len(all_samples)
        chart_blocks.append({
            "window": window,
            "canvas_id": f"priceChart_{window}",
            "labels": labels,
            "up_buy": up_buy_data,
            "up_sell": up_sell_data,
            "down_buy": down_buy_data,
            "down_sell": down_sell_data,
            "total_samples": total_samples,
            "market_count": len(window_data),
            "market_info": market_info,
        })

    total_samples_all = sum(c["total_samples"] for c in chart_blocks)
    total_markets_all = sum(c["market_count"] for c in chart_blocks)

    # === Build the chart sections ===
    chart_sections_html = ""
    chart_init_js = ""
    for block in chart_blocks:
        market_info = block["market_info"]
        market_legend_items = ""
        for slug, info in market_info.items():
            short_slug = slug[-18:] if len(slug) > 18 else slug
            market_legend_items += (
                '<div class="market-item">'
                f'<span class="market-dot" style="background-color: {info["color"]}"></span>'
                f'<span title="{slug}">{short_slug}</span>'
                '</div>\n'
            )
        chart_sections_html += f"""
    <div class="chart-section">
        <h2>📊 {block['window']} 窗口价格趋势
            <span class="chart-meta">
                {block['market_count']} market(s) · {block['total_samples']} sample(s)
            </span>
            <button class="reset-zoom-btn" id="reset_zoom_{block['canvas_id']}"
                    title="鼠标左键拖拽图表空白区域来放大；点此按钮重置">🔄 重置缩放</button>
        </h2>
        <div class="chart-hint">提示：按住鼠标左键在图表上拖拽以选中区域放大</div>
        <div class="market-legend">
            <strong>市场:</strong>
            {market_legend_items}
        </div>
        <div class="chart-container">
            <canvas id="{block['canvas_id']}"></canvas>
        </div>
    </div>
"""
        chart_init_js += f"""
        (function() {{
            const ctx = document.getElementById('{block['canvas_id']}');
            if (!ctx) return;
            const chart = new Chart(ctx, {{
                type: 'line',
                data: {{
                    labels: {block['labels']!r},
                    datasets: [
                        {{
                            label: 'UP Ask (买入)',
                            data: {block['up_buy']!r},
                            borderColor: '#28a745',
                            backgroundColor: 'rgba(40, 167, 69, 0.1)',
                            borderWidth: 2,
                            tension: 0.3,
                            fill: false,
                            pointRadius: 2,
                            pointHoverRadius: 4,
                            spanGaps: true,
                        }},
                        {{
                            label: 'UP Bid (卖出)',
                            data: {block['up_sell']!r},
                            borderColor: 'rgba(40, 167, 69, 0.5)',
                            borderWidth: 2,
                            tension: 0.3,
                            borderDash: [5, 5],
                            fill: false,
                            pointRadius: 1,
                            pointHoverRadius: 3,
                            spanGaps: true,
                        }},
                        {{
                            label: 'DOWN Ask (买入)',
                            data: {block['down_buy']!r},
                            borderColor: '#dc3545',
                            backgroundColor: 'rgba(220, 53, 69, 0.1)',
                            borderWidth: 2,
                            tension: 0.3,
                            fill: false,
                            pointRadius: 2,
                            pointHoverRadius: 4,
                            spanGaps: true,
                        }},
                        {{
                            label: 'DOWN Bid (卖出)',
                            data: {block['down_sell']!r},
                            borderColor: 'rgba(220, 53, 69, 0.5)',
                            borderWidth: 2,
                            tension: 0.3,
                            borderDash: [5, 5],
                            fill: false,
                            pointRadius: 1,
                            pointHoverRadius: 3,
                            spanGaps: true,
                        }},
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: {{ mode: 'index', intersect: false }},
                    plugins: {{
                        legend: {{
                            position: 'top',
                            labels: {{ usePointStyle: true, padding: 12 }}
                        }},
                        tooltip: {{
                            backgroundColor: 'rgba(0, 0, 0, 0.8)',
                            padding: 12
                        }}
                    }},
                    scales: {{
                        x: {{
                            title: {{ display: true, text: '时间 (UTC)' }},
                            ticks: {{
                                maxRotation: 45,
                                minRotation: 45,
                                maxTicksLimit: 20
                            }}
                        }},
                        y: {{
                            title: {{ display: true, text: '价格' }},
                            min: 0, max: 1,
                            ticks: {{ callback: v => v.toFixed(2) }}
                        }}
                    }},
                    plugins: {{
                        zoom: {{
                            zoom: {{
                                drag: {{
                                    enabled: true,
                                    backgroundColor: 'rgba(33, 150, 243, 0.18)',
                                    borderColor: 'rgba(33, 150, 243, 0.85)',
                                    borderWidth: 1,
                                    threshold: 4
                                }},
                                mode: 'x',
                                onZoomComplete: function(ctx) {{
                                    try {{ ctx.chart.options.scales.y.min = 0; ctx.chart.options.scales.y.max = 1; ctx.chart.update('none'); }} catch (e) {{}}
                                }}
                            }},
                            pan: {{
                                enabled: true,
                                mode: 'x',
                                modifierKey: 'shift'
                            }},
                            limits: {{
                                x: {{ min: 'original', max: 'original' }}
                            }}
                        }}
                    }}
                }}
            }});
            // Expose instance so the toolbar reset button can reach it
            // even though this chart is created inside an IIFE.
            try {{ window.__priceCharts = window.__priceCharts || {{}}; window.__priceCharts['{block['canvas_id']}'] = chart; }} catch (e) {{}}
        }})();
"""

    # === Down-stats table rows ===
    down_table_rows_html = ""
    for entry in down_stats_rows:
        ds = entry["stats"]
        final_bid = ds.get("final_down_bid")
        min_bid = ds.get("min_down_bid")
        reached_99 = ds.get("reached_99", False)
        below_45 = ds.get("reached_99_and_below_45", False)
        below_40 = ds.get("reached_99_and_below_40", False)
        below_30 = ds.get("reached_99_and_below_30", False)
        short_slug = entry["slug"][-20:] if len(entry["slug"]) > 20 else entry["slug"]
        final_str = f"{final_bid:.4f}" if final_bid else "N/A"
        min_str = f"{min_bid:.4f}" if min_bid else "N/A"

        def cls(v): return "yes" if v else "no"
        def txt(v): return "是" if v else "否"

        down_table_rows_html += (
            '<tr>'
            f'<td><span class="window-badge">{entry["window"]}</span></td>'
            f'<td><code title="{entry["slug"]}">{short_slug}</code></td>'
            f'<td>{final_str}</td>'
            f'<td>{min_str}</td>'
            f'<td class="{cls(reached_99)}">{txt(reached_99)}</td>'
            f'<td class="{cls(below_45)}">{txt(below_45)}</td>'
            f'<td class="{cls(below_40)}">{txt(below_40)}</td>'
            f'<td class="{cls(below_30)}">{txt(below_30)}</td>'
            '</tr>\n'
        )

    if not down_table_rows_html:
        down_table_rows_html = '<tr><td colspan="8" style="text-align:center;color:#888;">暂无数据</td></tr>'

    # Build browser_refresh_seconds into the template so the JS timer and
    # the display label are always in sync.
    if browser_refresh_seconds >= 3600:
        refresh_display = f"{browser_refresh_seconds // 3600} hr"
    elif browser_refresh_seconds >= 60:
        refresh_display = f"{browser_refresh_seconds // 60} min"
    else:
        refresh_display = f"{browser_refresh_seconds} sec"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <!-- No <meta http-equiv="refresh"> here — browser auto-reload is handled
         by a JS timer below so the interval can be configured at runtime. -->
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Polymarket 价格监控报告 - {', '.join(windows)}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.2.0/dist/chartjs-plugin-zoom.min.js"></script>
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
        .toolbar {{
            background: white;
            padding: 12px 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .toolbar .label {{ color: #666; font-size: 0.9em; }}
        .toolbar button {{
            background: #2196F3;
            color: white;
            border: none;
            padding: 8px 18px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.95em;
            font-weight: 500;
            transition: background 0.15s;
        }}
        .toolbar button:hover {{ background: #1976D2; }}
        .toolbar button:active {{ background: #0D47A1; }}
        .toolbar .last-refresh {{ color: #888; font-size: 0.85em; margin-left: auto; }}
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
        .chart-meta {{
            font-size: 0.55em;
            color: #888;
            font-weight: normal;
            margin-left: 12px;
        }}
        .reset-zoom-btn {{
            float: right;
            font-size: 0.55em;
            background: #6c757d;
            color: white;
            border: none;
            padding: 6px 14px;
            border-radius: 4px;
            cursor: pointer;
            font-weight: 500;
            transition: background 0.15s;
        }}
        .reset-zoom-btn:hover {{ background: #5a6268; }}
        .reset-zoom-btn:active {{ background: #484e53; }}
        .chart-hint {{
            font-size: 0.82em;
            color: #666;
            background: #f0f7ff;
            border-left: 3px solid #2196F3;
            padding: 6px 12px;
            margin: 10px 0;
            border-radius: 4px;
        }}
        .browser-reload-badge {{
            font-size: 0.78em;
            color: #666;
            padding: 4px 10px;
            background: #f5f5f5;
            border-radius: 12px;
            border: 1px solid #ddd;
            margin-left: 8px;
        }}
        .chart-container {{
            position: relative;
            height: 380px;
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
        .down-stats {{ margin-top: 30px; }}
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
        .down-stats th {{ background: #f8f9fa; font-weight: 600; }}
        .down-stats .yes {{ color: #28a745; font-weight: bold; }}
        .down-stats .no {{ color: #dc3545; font-weight: bold; }}
        .window-badge {{
            display: inline-block;
            background: #2196F3;
            color: white;
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 0.8em;
            font-weight: 600;
        }}
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

    <div class="toolbar">
        <span class="label">📊 监控窗口: {', '.join(windows)}</span>
        <button onclick="manualRefresh()">🔄 手动刷新</button>
        <span class="last-refresh" id="lastRefresh">页面加载时间: 加载中…</span>
        <span class="browser-reload-badge" id="browserReloadBadge" style="display:none;"></span>
    </div>

    <div class="summary">
        <h2>概要</h2>
        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-label">交易品种</div>
                <div class="stat-value">{asset}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">监控窗口</div>
                <div class="stat-value">{', '.join(windows)}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">运行ID</div>
                <div class="stat-value">{run_id}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">市场总数 (跨窗口)</div>
                <div class="stat-value">{total_markets_all}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">采样总数 (跨窗口)</div>
                <div class="stat-value">{total_samples_all}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">采样间隔</div>
                <div class="stat-value">{interval}s</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">页面刷新</div>
                <div class="stat-value">{refresh_display} (auto)</div>
            </div>
        </div>
    </div>

    <div class="down-stats">
        <h3>📉 DOWN 价格统计 (所有窗口)</h3>
        <table>
            <thead>
                <tr>
                    <th>窗口</th>
                    <th>市场</th>
                    <th>结束 Bid</th>
                    <th>最低 Bid</th>
                    <th>达 0.99?</th>
                    <th>且曾&lt;0.45?</th>
                    <th>且曾&lt;0.40?</th>
                    <th>且曾&lt;0.30?</th>
                </tr>
            </thead>
            <tbody>
{down_table_rows_html}            </tbody>
        </table>
    </div>

{chart_sections_html}
    <script>
        Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
{chart_init_js}

        // === Browser auto-reload timer ===
        // Reloads the page every N seconds to pick up the latest HTML snapshot
        // written by the monitor process.  The interval is injected at generation
        // time so the display label and the actual timer are always in sync.
        (function() {{
            const REFRESH_SECS = {browser_refresh_seconds};

            // Countdown badge — updated every second so the user knows when
            // the next reload will happen.
            var _remaining = REFRESH_SECS;
            setInterval(function() {{
                _remaining -= 1;
                var badge = document.getElementById('browserReloadBadge');
                if (badge) {{
                    if (_remaining > 0) {{
                        var m = Math.floor(_remaining / 60);
                        var s = _remaining % 60;
                        badge.textContent = '⏳ ' + (m > 0 ? m + 'm ' : '') + s + 's 后自动刷新';
                        badge.style.display = '';
                    }} else {{
                        badge.textContent = '🔄 正在刷新...';
                    }}
                }}
                if (_remaining <= 0) {{
                    _remaining = REFRESH_SECS;  // reset for next cycle
                    window.location.reload(true);
                }}
            }}, 1000);
        }})();

        // === Manual refresh button ===
        // Force the browser to refetch live.html from disk (does NOT restart
        // the sampling loop; only reloads the latest snapshot).
        function manualRefresh() {{
            const btn = event && event.target;
            if (btn) {{
                btn.disabled = true;
                btn.textContent = '⏳ 刷新中...';
            }}
            // Cache-bust to ensure we get the latest file from disk.
            window.location.reload(true);
        }}

        // Bind reset-zoom buttons for every chart on the page.
        (function() {{
            const charts = window.__priceCharts || {{}};
            document.querySelectorAll('.reset-zoom-btn').forEach(function(b) {{
                b.addEventListener('click', function(e) {{
                    // Button id is "reset_zoom_<canvas_id>"; strip the prefix.
                    const canvasId = b.id.replace(/^reset_zoom_/, '');
                    const ch = charts[canvasId];
                    if (ch && typeof ch.resetZoom === 'function') {{
                        ch.resetZoom();
                    }}
                }});
            }});
        }})();

        // Update "last refresh" label
        (function() {{
            const now = new Date();
            const pad = n => String(n).padStart(2, '0');
            const stamp = `${{now.getFullYear()}}-${{pad(now.getMonth()+1)}}-${{pad(now.getDate())}} ${{pad(now.getHours())}}:${{pad(now.getMinutes())}}:${{pad(now.getSeconds())}}`;
            const el = document.getElementById('lastRefresh');
            if (el) el.textContent = '页面加载时间: ' + stamp + ' (本地时区)';
        }})();
    </script>

    <div class="footer">
        <p>由 poly-simmer-fast-loop 价格监控生成</p>
        <p>运行ID: {run_id}</p>
    </div>
</body>
</html>
"""

    with open(report_path, "w") as f:
        f.write(html)


if __name__ == "__main__":
    main()
