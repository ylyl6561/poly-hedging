"""
Pre-open market monitor: per-minute polling, event discovery, pool maintenance.

Discovers BTC 5m markets, normalizes them into PreopenEvents,
upserts into the shared pool (with refreshed market health flags),
and runs GC on stale / untradeable events.
"""

from datetime import datetime, timezone

from market import discover_fast_market_markets, parse_resolves_at
from .preopen_event_pool import PreopenEventPool, PreopenEvent, EventState


def make_preopen_event(market: dict, now: datetime) -> PreopenEvent | None:
    """
    Convert a market dict (from discover_fast_market_markets) into a PreopenEvent.

    Returns None if the market is invalid or already started/resolved.
    """
    condition_id = market.get("condition_id") or ""
    if not condition_id:
        return None

    slug = market.get("slug") or condition_id
    question = market.get("question") or slug
    source = market.get("source", "unknown")

    end_time = market.get("end_time")
    if end_time is None:
        return None

    # Normalize end_time
    if isinstance(end_time, str):
        end_time = parse_resolves_at(end_time)
    if end_time is None:
        return None

    # Derive start_time = end_time - window_seconds (5m = 300s)
    window_seconds = 300
    start_time = datetime.fromtimestamp(
        end_time.timestamp() - window_seconds, tz=timezone.utc
    )

    clob_token_ids = market.get("clob_token_ids") or []
    if len(clob_token_ids) < 2:
        return None

    fee_rate_bps = int(market.get("fee_rate_bps") or 0)

    # Extract market health flags from discovery data
    is_closed = bool(market.get("closed") or market.get("marketClosed") or market.get("accepting_orders") is False)
    is_resolved = bool(market.get("resolved") or market.get("settled"))

    event = PreopenEvent(
        condition_id=str(condition_id),
        slug=str(slug),
        question=str(question),
        start_time=start_time,
        end_time=end_time,
        clob_token_ids=[str(t) for t in clob_token_ids],
        fee_rate_bps=fee_rate_bps,
        source=str(source),
        is_closed=is_closed,
        is_resolved=is_resolved,
    )
    return event


def build_event_pool(
    pool: PreopenEventPool,
    asset: str,
    window: str,
    lead_time_sec: float,
    gc_grace_sec: float,
    now: datetime | None = None,
) -> tuple[list[PreopenEvent], list[PreopenEvent], int]:
    """
    One full polling cycle:
      1. Discover BTC 5m markets
      2. Normalize → upsert into pool (with refreshed market health flags)
      3. GC stale / untradeable events
      4. Return (discovered_events, gc_removed_events, pool_size)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    window_seconds = 300  # 5 分钟窗口

    # ── 1. Discover markets ───────────────────────────────────────────────────
    markets = discover_fast_market_markets(asset=asset, window=window, use_simmer=True)

    discovered = []
    skipped_invalid = 0
    skipped_tts_invalid = 0  # tts <= 0 或 tts > lead_time
    skipped_no_condition_id = 0
    skipped_no_tokens = 0
    skipped_already_ordered = 0

    # ── Debug: 打印发现的所有市场 ──────────────────────────────────────────
    print(f"\n  🔍 【市场发现】共找到 {len(markets)} 个市场")

    all_market_times = []  # 收集所有市场的时间信息用于汇总显示

    # 已下单事件集合（用于在【市场发现】中排除）
    ordered_condition_ids: set[str] = set()
    for e in pool.list_all():
        if e.state in (EventState.YES_PLACED, EventState.DOWN_RESTING, EventState.DOWN_SWITCHED) or e.yes_order_id or e.down_order_id:
            ordered_condition_ids.add(e.condition_id)

    for m in markets:
        # ── Filter: must be within lead-time and not yet started ──────────────
        end_time = m.get("end_time")
        if end_time is None:
            skipped_invalid += 1
            continue
        if isinstance(end_time, str):
            end_time = parse_resolves_at(end_time)
        if end_time is None:
            skipped_invalid += 1
            continue

        start_time = datetime.fromtimestamp(
            end_time.timestamp() - window_seconds, tz=timezone.utc
        )
        tts = (start_time - now).total_seconds()
        remaining = (end_time - now).total_seconds()

        # 详细过滤日志
        condition_id = m.get("condition_id") or ""
        clob_token_ids = m.get("clob_token_ids") or []

        # 收集时间信息（时间表也带上 condition_id，方便标记“已下单”）
        market_info = {
            "condition_id": condition_id,
            "slug": m.get("slug", "")[:40],
            "start_time": start_time,
            "end_time": end_time,
            "tts": tts,
            "remaining": remaining,
            "source": m.get("source", "unknown"),
        }
        all_market_times.append(market_info)

        if not condition_id:
            skipped_no_condition_id += 1
            print(f"     ├─ ❌ 跳过 [无 condition_id]: {m.get('slug', '')[:30]}")
            continue

        # 已下单的事件：在【市场发现】中排除，避免重复刷屏
        if condition_id in ordered_condition_ids:
            skipped_already_ordered += 1
            continue

        if not clob_token_ids or len(clob_token_ids) < 2:
            skipped_no_tokens += 1
            print(f"     ├─ ❌ 跳过 [无交易对]: {m.get('slug', '')[:30]}")
            continue

        if tts <= 0:
            skipped_tts_invalid += 1
            print(
                f"     ├─ ❌ 过滤 [已开始] {m.get('slug', '')[:30]} "
                f"| 开始于 {start_time.strftime('%H:%M:%S')} UTC | tts={tts:.0f}s"
            )
            continue

        if tts > lead_time_sec:
            skipped_tts_invalid += 1
            print(
                f"     ├─ ❌ 过滤 [太远] {m.get('slug', '')[:30]} "
                f"| 开始于 {start_time.strftime('%H:%M:%S')} UTC | tts={tts:.0f}s > {lead_time_sec}s"
            )
            continue

        # 通过所有过滤 → 交易事件
        print(
            f"     └─ ✅ 可交易 {m.get('slug', '')[:30]} "
            f"| 开始 {start_time.strftime('%H:%M:%S')} UTC "
            f"| 结束 {end_time.strftime('%H:%M:%S')} UTC "
            f"| tts={tts:.0f}s"
        )

        # ── Build PreopenEvent using the helper ────────────────────────────────
        event = make_preopen_event(m, now)
        if event is None:
            skipped_invalid += 1
            continue

        # ── Upsert: refresh market health flags for existing events ─────────────
        existing = pool.get(event.condition_id)
        if existing is not None:
            # Update health flags from latest discovery data
            existing.update_market_health(
                closed=event.is_closed,
                resolved=event.is_resolved,
            )
            # Also refresh times (may have been corrected)
            existing.start_time = event.start_time
            existing.end_time = event.end_time
        else:
            pool.add(event)
            discovered.append(event)

    # ── 2. GC stale / untradeable events ────────────────────────────────────
    removed = pool.gc(now, gc_grace_sec)

    # ── 3. 打印汇总信息 ──────────────────────────────────────────────────────
    print(f"\n  📊 【汇总】发现 {len(markets)} 个 | 可交易 {len(discovered)} 个")
    if skipped_invalid > 0:
        print(f"     ├─ 无效数据跳过: {skipped_invalid}")
    if skipped_no_condition_id > 0:
        print(f"     ├─ 无 condition_id: {skipped_no_condition_id}")
    if skipped_already_ordered > 0:
        print(f"     ├─ 已下单事件排除: {skipped_already_ordered}")
    if skipped_no_tokens > 0:
        print(f"     ├─ 无交易对: {skipped_no_tokens}")
    if skipped_tts_invalid > 0:
        print(f"     └─ 时间窗口外: {skipped_tts_invalid}")

    # ── 4. 打印可交易事件时间表 ─────────────────────────────────────────────
    if all_market_times:
        print(f"\n  📅 【所有市场时间表】(当前: {now.strftime('%H:%M:%S')} UTC)")
        # 按 start_time 排序
        all_market_times.sort(key=lambda x: x["start_time"])

        for mi in all_market_times:
            tts = mi["tts"]
            remaining = mi["remaining"]

            ordered_marker = ""
            cid = mi.get("condition_id")
            if cid and cid in ordered_condition_ids:
                ordered_marker = " 🧾已下单"

            if tts <= 0:
                status = "🔴 已开始" + ordered_marker
                tts_str = f"已过 {-tts:.0f}s"
            elif tts <= lead_time_sec:
                status = "✅ 可交易" + ordered_marker
                tts_str = f"{tts:.0f}s后"
            else:
                status = "🔜 太远" + ordered_marker
                tts_str = f"{tts:.0f}s后"

            if remaining <= 0:
                remaining_str = "已结束"
            else:
                remaining_str = f"{remaining:.0f}s后结束"

            print(
                f"     {status} {mi['start_time'].strftime('%H:%M:%S')} - {mi['end_time'].strftime('%H:%M:%S')} UTC "
                f"| {mi['slug'][:35]} | 开始{tts_str} | {remaining_str}"
            )

    return discovered, removed, len(pool)


def format_event_summary(event: PreopenEvent, now: datetime) -> str:
    tts = (event.start_time - now).total_seconds()
    ttl = (event.end_time - now).total_seconds()
    tts_str = f"{tts:.0f}s" if tts > 0 else "已开始"
    ttl_str = f"{ttl:.0f}s" if ttl > 0 else "已结束"
    health = ""
    if event.is_closed:
        health += " 已关闭"
    if event.is_resolved:
        health += " 已结算"
    if not event.clob_token_ids or len(event.clob_token_ids) < 2:
        health += " 不可交易"
    state_map = {
        "discovered": "已发现",
        "ready": "就绪",
        "yes_placed": "YES已下单",
        "down_resting": "NO挂单中",
        "down_switched": "NO已切换",
        "closed": "已关闭",
    }
    state_cn = state_map.get(event.state.value, event.state.value)
    return (
        f"{event.slug[:35]}{health} | 状态={state_cn} | 距开始={tts_str} | 距结束={ttl_str}"
    )
