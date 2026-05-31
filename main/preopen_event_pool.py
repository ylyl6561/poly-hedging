"""
Pre-open event pool with lifecycle state machine.

Event states:
  DISCOVERED → READY → YES_PLACED → DOWN_RESTING → DOWN_SWITCHED → CLOSED

Key: condition_id
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class EventState(Enum):
    DISCOVERED = "discovered"      # Found in market discovery
    READY = "ready"                # Within lead-time window, eligible for trading
    YES_PLACED = "yes_placed"      # YES main order submitted
    DOWN_RESTING = "down_resting"  # Down GTC PostOnly resting
    DOWN_SWITCHED = "down_switched" # Down switched to FAK near open
    CLOSED = "closed"              # Event expired/resolved, pending GC


# Valid state transitions (current → allowed next states)
VALID_TRANSITIONS = {
    EventState.DISCOVERED: {EventState.READY, EventState.CLOSED},
    EventState.READY: {EventState.YES_PLACED, EventState.CLOSED},
    EventState.YES_PLACED: {EventState.DOWN_RESTING, EventState.CLOSED},
    EventState.DOWN_RESTING: {EventState.DOWN_SWITCHED, EventState.CLOSED},
    EventState.DOWN_SWITCHED: {EventState.CLOSED},
    EventState.CLOSED: set(),  # Terminal
}


@dataclass
class PreopenEvent:
    """One BTC 5m event tracked in the pre-open pool."""
    condition_id: str
    slug: str
    question: str
    start_time: datetime          # UTC, when the 5m window starts
    end_time: datetime            # UTC, when the 5m window ends
    clob_token_ids: list          # [yes_token, no_token]
    fee_rate_bps: int = 0
    source: str = "unknown"       # "gamma_deterministic", "gamma", "simmer"

    state: EventState = EventState.DISCOVERED
    yes_order_id: Optional[str] = None
    down_order_id: Optional[str] = None
    yes_fill_price: Optional[float] = None
    down_fill_price: Optional[float] = None
    action_count: int = 0          # Number of state transitions attempted

    # Market health flags — updated on each discovery refresh
    is_closed: bool = False
    is_resolved: bool = False

    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_action_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_refresh_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def can_transition(self, target: EventState) -> bool:
        return target in VALID_TRANSITIONS.get(self.state, set())

    def transition_to(self, target: EventState) -> bool:
        """Attempt state transition. Returns True if successful."""
        if not self.can_transition(target):
            return False
        self.state = target
        self.action_count += 1
        self.last_action_at = datetime.now(timezone.utc)
        return True

    def time_to_start(self, now: datetime) -> float:
        """Seconds until start_time. Negative if already started."""
        return (self.time_to_start_dt(now).total_seconds())

    def time_to_start_dt(self, now: datetime) -> datetime:
        return (self.start_time - now)

    def time_to_end(self, now: datetime) -> float:
        """Seconds until end_time. Negative if already ended."""
        return (self.end_time - now).total_seconds()

    def is_started(self, now: datetime) -> bool:
        return (self.start_time - now).total_seconds() <= 0

    def is_ended(self, now: datetime) -> bool:
        return (self.end_time - now).total_seconds() <= 0

    def is_tradeable(self, now: datetime, lead_time_sec: float) -> bool:
        """Event is in the pre-open window: not started yet, within lead_time, and tradeable."""
        if self.is_closed or self.is_resolved:
            return False
        if not self.clob_token_ids or len(self.clob_token_ids) < 2:
            return False
        if self.is_started(now):
            return False
        tts = (self.start_time - now).total_seconds()
        return 0 < tts <= lead_time_sec

    def update_market_health(self, closed: bool = False, resolved: bool = False):
        """Refresh market health flags from latest discovery data."""
        self.is_closed = closed
        self.is_resolved = resolved
        self.last_refresh_at = datetime.now(timezone.utc)

    def to_dict(self):
        return {
            "condition_id": self.condition_id,
            "slug": self.slug,
            "question": self.question,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "clob_token_ids": self.clob_token_ids,
            "fee_rate_bps": self.fee_rate_bps,
            "source": self.source,
            "state": self.state.value,
            "yes_order_id": self.yes_order_id,
            "down_order_id": self.down_order_id,
            "yes_fill_price": self.yes_fill_price,
            "down_fill_price": self.down_fill_price,
            "action_count": self.action_count,
            "is_closed": self.is_closed,
            "is_resolved": self.is_resolved,
            "discovered_at": self.discovered_at.isoformat(),
            "last_action_at": self.last_action_at.isoformat(),
            "last_refresh_at": self.last_refresh_at.isoformat(),
        }


class PreopenEventPool:
    """In-memory event pool keyed by condition_id."""

    def __init__(self):
        self._events: dict[str, PreopenEvent] = {}

    def __len__(self):
        return len(self._events)

    def __contains__(self, condition_id: str) -> bool:
        return condition_id in self._events

    def get(self, condition_id: str) -> Optional[PreopenEvent]:
        return self._events.get(condition_id)

    def add(self, event: PreopenEvent) -> PreopenEvent:
        """Upsert: add if new, update if existing (only non-terminal fields)."""
        existing = self._events.get(event.condition_id)
        if existing is None:
            self._events[event.condition_id] = event
            return event
        # Update non-terminal fields that may have changed (e.g., latest prices)
        existing.end_time = event.end_time
        existing.start_time = event.start_time
        return existing

    def remove(self, condition_id: str) -> PreopenEvent | None:
        return self._events.pop(condition_id, None)

    def list_all(self) -> list[PreopenEvent]:
        return list(self._events.values())

    def list_by_state(self, state: EventState) -> list[PreopenEvent]:
        return [e for e in self._events.values() if e.state == state]

    def list_tradeable(self, now: datetime, lead_time_sec: float) -> list[PreopenEvent]:
        """Events in the pre-open window: not started, within lead_time, tradeable."""
        return [
            e for e in self._events.values()
            if e.state not in (EventState.CLOSED,)
            and e.is_tradeable(now, lead_time_sec)
        ]

    def select_nearest(self, now: datetime, lead_time_sec: float) -> PreopenEvent | None:
        """Return the nearest unstarted event within the lead-time window."""
        candidates = self.list_tradeable(now, lead_time_sec)
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.start_time)
        return candidates[0]

    def gc(self, now: datetime, grace_sec: float) -> list[PreopenEvent]:
        """
        Remove expired / untradeable events.

        GC conditions (any one triggers removal):
          1. Already in CLOSED state
          2. Market is closed or resolved
          3. Missing clob_token_ids or fewer than 2 tokens
          4. Past end_time + grace period
          5. Started long ago + grace period

        Returns list of removed events.
        """
        removed = []
        for cid in list(self._events.keys()):
            e = self._events[cid]

            # Condition 1: already terminal
            if e.state == EventState.CLOSED:
                removed.append(self._events.pop(cid))
                continue

            # Condition 2: market health — closed / resolved
            if e.is_closed or e.is_resolved:
                e.transition_to(EventState.CLOSED)
                removed.append(self._events.pop(cid))
                continue

            # Condition 3: missing tokens (cannot trade)
            if not e.clob_token_ids or len(e.clob_token_ids) < 2:
                e.transition_to(EventState.CLOSED)
                removed.append(self._events.pop(cid))
                continue

            # Condition 4: past end time + grace
            if (e.end_time - now).total_seconds() < -grace_sec:
                e.transition_to(EventState.CLOSED)
                removed.append(self._events.pop(cid))
                continue

            # Condition 5: started long ago + grace
            if (e.start_time - now).total_seconds() < -grace_sec:
                e.transition_to(EventState.CLOSED)
                removed.append(self._events.pop(cid))
                continue

        return removed

    def to_dict(self) -> dict:
        return {cid: e.to_dict() for cid, e in self._events.items()}
