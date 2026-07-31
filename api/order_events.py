"""
Order-event bus for the smart-money copy-trading pipeline.

Why this exists
---------------
Copy-trading has a hard real-time requirement: when the leader opens,
adjusts, or closes a position, the follower (this bot) must react in
seconds.  When the leader's order *fails* — the buy never settled, the
size was wrong, the market moved — the follower must be told *why* and
what to do, not silently swallow the error.

This module gives the rest of the system a uniform way to:

* publish order state transitions on a topic
* subscribe by topic / by leader wallet
* pull a snapshot of the last known state for any order id
* export the same data to the dashboard so users can see exactly what
  the bot is doing and why.

States follow the lifecycle of a single copy-trade decision:

    PENDING     — leader's order detected, follower hasn't acted
    INFLIGHT    — follower's order sent to CLOB
    FILLED      — fill confirmed on-chain / via /data/trades
    PARTIAL     — partial fill (requires user attention)
    CANCELLED   — order cancelled before fill
    FAILED      — send or fill failed; error reason recorded
    SKIPPED     — leader signal intentionally not followed
    MIRRORED    — follower matched the leader and we reported back

``MIRRORED`` is the steady-state happy path.  Anything else surfaces in
the dashboard with the reason attached.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class OrderStatus(str, Enum):
    PENDING = "pending"
    INFLIGHT = "inflight"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"
    SKIPPED = "skipped"
    MIRRORED = "mirrored"


# Statuses that require *human attention* (highlighted in the dashboard).
ATTENTION_STATUSES: frozenset[OrderStatus] = frozenset(
    {OrderStatus.PARTIAL, OrderStatus.FAILED, OrderStatus.SKIPPED}
)


@dataclass
class OrderEvent:
    """A single state-transition or context update for a copy-trade order."""

    event_id: str
    order_id: str              # our local copy-trade id, stable
    leader_wallet: str         # wallet we are following
    market_id: str | None      # Polymarket condition_id, when known
    asset_id: str | None       # CLOB token_id, when known
    side: str | None           # "BUY" / "SELL" / None for context events
    status: OrderStatus
    reason: str | None = None  # human-readable explanation / error message
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=lambda: time.time())

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, ensure_ascii=False, default=str)


# ============================================================================
# In-process pub/sub
# ============================================================================


SubscriberId = str


class OrderEventBus:
    """Thread-safe pub/sub with topic filtering and a bounded snapshot log.

    Topics are simple dotted strings, e.g. ``"order:0xabc..."``,
    ``"wallet:0xabc..."``, ``"market:0xCOND..."``.  Subscribers may
    subscribe to one topic or a list; receiving ``None`` means "all".

    Subscribers can also opt to receive the *snapshot* (recent events)
    on connect by setting ``send_snapshot=True``.  This is what the
    dashboard uses so newly-loaded tabs see the full state immediately.
    """

    def __init__(self, *, snapshot_limit: int = 500) -> None:
        self._subscribers: dict[SubscriberId, Callable[[OrderEvent], None]] = {}
        self._subscriptions: dict[SubscriberId, set[str | None]] = defaultdict(
            set
        )
        self._snapshot: deque[OrderEvent] = deque(maxlen=snapshot_limit)
        # order_id -> latest event (for "current state of this order")
        self._latest_by_order: dict[str, OrderEvent] = {}
        self._lock = threading.RLock()
        self._next_id = 1

    # ---- subscribe ----

    def subscribe(
        self,
        callback: Callable[[OrderEvent], None],
        *,
        topics: Iterable[str] | None = None,
        send_snapshot: bool = False,
    ) -> SubscriberId:
        """Register ``callback`` for the given topics (or all if ``None``).

        Returns a subscriber id; pass to :meth:`unsubscribe` to remove.
        If ``send_snapshot`` is set, the bus will replay the most recent
        snapshot events to the subscriber immediately (best-effort, in a
        background thread so the caller is not blocked).
        """
        with self._lock:
            sub_id = f"sub-{self._next_id:06d}"
            self._next_id += 1
            self._subscribers[sub_id] = callback
            if topics is None:
                self._subscriptions[sub_id].add(None)
            else:
                for t in topics:
                    self._subscriptions[sub_id].add(t)
        if send_snapshot and self._snapshot:
            snapshot = list(self._snapshot)
            self._deliver_async(callback, snapshot)
        logger.debug("order bus subscriber added id=%s topics=%s", sub_id, topics)
        return sub_id

    def unsubscribe(self, subscriber_id: SubscriberId) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)
            self._subscriptions.pop(subscriber_id, None)

    # ---- publish ----

    def publish(self, event: OrderEvent) -> None:
        """Publish an event.  Updates the snapshot and per-order cache
        before fanning out to subscribers.

        Also writes a row to the ``smart_money_order_events`` table so
        cross-process dashboards (worker → serve) can read events back
        via ``/api/order-events/recent``.  The DB write is wrapped in
        a try/except so an SQL hiccup never blocks the in-memory
        subscribers.
        """
        if not event.event_id:
            event.event_id = uuid.uuid4().hex
        with self._lock:
            self._snapshot.append(event)
            self._latest_by_order[event.order_id] = event
            targets = [
                (sub_id, cb)
                for sub_id, cb in self._subscribers.items()
                if self._matches(event, self._subscriptions.get(sub_id, set()))
            ]
        # Deliver outside the lock.
        for _, cb in targets:
            try:
                cb(event)
            except Exception:  # pragma: no cover — defensive
                logger.exception("order bus subscriber raised")
        # Mirror to PostgreSQL so the dashboard can read events that
        # were published from another process (worker ↔ serve).
        try:
            self._persist_event(event)
        except Exception:  # pragma: no cover — defensive
            logger.exception("order bus persist failed order_id=%s", event.order_id)

    # ---- query ----

    def latest(self, order_id: str) -> OrderEvent | None:
        with self._lock:
            return self._latest_by_order.get(order_id)

    def snapshot(self, limit: int = 200) -> list[OrderEvent]:
        with self._lock:
            return list(self._snapshot)[-limit:]

    def attention_required(self, limit: int = 100) -> list[OrderEvent]:
        with self._lock:
            return [
                ev
                for ev in self._snapshot
                if ev.status in ATTENTION_STATUSES
            ][-limit:]

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "subscribers": len(self._subscribers),
                "snapshot_size": len(self._snapshot),
                "tracked_orders": len(self._latest_by_order),
            }

    # ---- helpers ----

    @staticmethod
    def _matches(event: OrderEvent, topics: set[str | None]) -> bool:
        if None in topics:
            return True
        for topic in topics:
            if topic.startswith("order:"):
                if event.order_id == topic[len("order:") :]:
                    return True
            elif topic.startswith("wallet:"):
                if event.leader_wallet == topic[len("wallet:") :]:
                    return True
            elif topic.startswith("market:"):
                if event.market_id == topic[len("market:") :]:
                    return True
            elif topic == f"status:{event.status.value}":
                return True
        return False

    @staticmethod
    def _persist_event(event: OrderEvent) -> None:
        """Upsert a row into ``smart_money_order_events``.

        We **upsert by ``order_id``** rather than appending, because the
        executor publishes multiple events for the same order lifecycle
        ("signal approved" → "awaiting human approval" → "SEMI-AUTO
        approval required"), each with the same ``status='pending'``.
        The dashboard should only see the *latest* state per order, not
        three rows saying "pending".  Appending all of them would
        produce the duplicate event rows the user complained about.

        Strategy: keep at most one row per ``order_id``.  When the
        executor transitions to a new status (e.g. ``inflight`` →
        ``filled``), we **update the existing row's status / reason /
        data**, preserving the original row id (so the dashboard's
        polling cursor remains stable).  If the executor ever moves
        backward (rare — usually a final state replacing an
        intermediate), we still replace the row.

        Imports are local so the bus can be constructed without a
        working DB (e.g. in unit tests that don't care about persistent
        history).  We deliberately do NOT propagate DB errors — the
        in-memory bus is the source of truth for the process that
        produced the event.
        """
        try:
            from smart_money.db import session_scope
            from smart_money.models import OrderEventLog
            from sqlalchemy import select
        except Exception:
            return
        try:
            with session_scope() as session:
                # Find an existing row for this order_id.  We do NOT
                # update the *oldest* row — we update the most recent
                # so the rolling cursor advances in id order, which
                # keeps the dashboard's ``since_id`` polling correct.
                existing = session.execute(
                    select(OrderEventLog)
                    .where(OrderEventLog.order_id == event.order_id)
                    .order_by(OrderEventLog.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if existing is not None:
                    existing.event_id = event.event_id or existing.event_id
                    existing.leader_wallet = event.leader_wallet or ""
                    existing.market_id = event.market_id
                    existing.asset_id = event.asset_id
                    existing.side = event.side
                    existing.status = event.status.value
                    existing.reason = event.reason
                    existing.data = event.data or {}
                    existing.ts = float(event.ts)
                    existing.created_at = datetime.now(timezone.utc)
                else:
                    session.add(OrderEventLog(
                        event_id=event.event_id,
                        order_id=event.order_id,
                        leader_wallet=event.leader_wallet or "",
                        market_id=event.market_id,
                        asset_id=event.asset_id,
                        side=event.side,
                        status=event.status.value,
                        reason=event.reason,
                        data=event.data or {},
                        ts=float(event.ts),
                    ))
        except Exception:
            logger.exception("OrderEventLog upsert failed")

    @staticmethod
    def _deliver_async(
        callback: Callable[[OrderEvent], None], events: list[OrderEvent]
    ) -> None:
        # Snapshot replay must not block the publisher; in a real-time
        # dashboard we typically deliver over a websocket, but the bus
        # itself stays decoupled from any transport.
        for ev in events:
            try:
                callback(ev)
            except Exception:  # pragma: no cover
                logger.exception("snapshot replay raised")


# ============================================================================
# Order id helpers
# ============================================================================


def new_order_id(leader_wallet: str, market_id: str | None = None) -> str:
    """Generate a stable, traceable order id.

    Format: ``ct-{leader_short}-{nonce}``.  The leader prefix makes
    filtering easy in logs; the nonce keeps uniqueness even when the
    same leader fires multiple orders in the same second.
    """
    short = leader_wallet.lower()
    if short.startswith("0x"):
        short = short[2:]
    short = short[:8]
    nonce = uuid.uuid4().hex[:8]
    suffix = f"-{market_id[:6]}" if market_id else ""
    return f"ct-{short}{suffix}-{nonce}"


# ============================================================================
# Process-wide singleton
# ============================================================================


_default_bus_lock = threading.Lock()
_default_bus: OrderEventBus | None = None


def get_order_event_bus() -> OrderEventBus:
    global _default_bus
    with _default_bus_lock:
        if _default_bus is None:
            _default_bus = OrderEventBus()
        return _default_bus


def reset_default_order_event_bus() -> None:
    global _default_bus
    with _default_bus_lock:
        _default_bus = None


__all__ = [
    "ATTENTION_STATUSES",
    "OrderEvent",
    "OrderEventBus",
    "OrderStatus",
    "get_order_event_bus",
    "new_order_id",
    "reset_default_order_event_bus",
]