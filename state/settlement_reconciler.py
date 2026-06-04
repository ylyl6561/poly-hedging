"""Settlement reconciler for preopen trades CSV.

Goal:
- Every 2 minutes, scan the current run folder's `trades.csv`.
- For rows with `settled` missing/false and a `condition_id`, fetch outcome.
- Once settled, update `trades.xlsx` in place with authoritative final PnL when
  available, and also keep an append-only CSV audit row.

Design choices:
- Excel is the primary human-readable report.
- CSV remains append-only for audit/replay.
- De-dup: maintains a small JSON state file in run folder to avoid repeatedly
  processing the same settlement.

This module is best-effort: it should never crash the main trading loop.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from api.api import api_request, fetch_market_outcome
from state.excel_trade_updater import update_trade_row
from state.reconcile_export import TradeRecord, _append_to_csv


def _now_local_hms() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")


def _load_rows(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y", "on"}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        text = str(value).strip()
        if text == "":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _settlement_state_path(run_folder: Path) -> Path:
    return run_folder / "settlement_state.json"


def _load_state(run_folder: Path) -> dict[str, Any]:
    path = _settlement_state_path(run_folder)
    if not path.exists():
        return {"schema_version": 1, "settled": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "settled": {}}


def _save_state(run_folder: Path, state: dict[str, Any]) -> None:
    path = _settlement_state_path(run_folder)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _row_key(row: dict[str, Any]) -> str | None:
    cid = (row.get("condition_id") or "").strip()
    side = (row.get("side") or "").strip().upper()
    ts = (row.get("recorded_at_utc") or "").strip()
    if not cid or not side or not ts:
        return None
    return f"{cid}:{side}:{ts}"


def _compute_final_pnl(*, side: str, shares: float, price: float, outcome: str) -> float:
    win_side = str(outcome).strip().upper()
    payout = shares if win_side == side else 0.0
    cost = shares * price
    return payout - cost


def _compute_final_pnl_from_cash_pnl(
    *,
    wallet: str | None,
    condition_id: str,
) -> tuple[float | None, str | None]:
    """Prefer authoritative cashPnl from Data API when available."""
    if not wallet:
        return None, None
    try:
        url = f"https://data-api.polymarket.com/positions?user={wallet}&market={condition_id}"
        payload = api_request(url, timeout=10)
    except Exception:
        return None, None

    positions: list[dict[str, Any]] = []
    if isinstance(payload, list):
        positions = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        positions = payload["data"]
    else:
        return None, None

    for pos in positions:
        if str(pos.get("conditionId") or pos.get("condition_id") or "") != str(condition_id):
            continue
        cash = pos.get("cashPnl")
        if cash is None:
            cash = pos.get("cash_pnl")
        cash_val = _safe_float(cash)
        if cash_val is not None:
            return cash_val, "data_api.cashPnl"

    return None, None


def reconcile_once(*, run_folder: str | Path, wallet_address: str | None = None) -> int:
    """Run one reconciliation pass.

    Behavior:
    - Updates `trades.xlsx` in place when a matching row exists.
    - Also appends a settlement row to `trades.csv` for auditability.

    Returns number of settlements processed.
    """
    run_folder = Path(run_folder)
    csv_path = run_folder / "trades.csv"
    xlsx_path = run_folder / "trades.xlsx"
    rows = _load_rows(csv_path)
    if not rows:
        return 0

    state = _load_state(run_folder)
    settled_state: dict[str, Any] = state.setdefault("settled", {})

    processed = 0
    for row in rows:
        if _truthy(row.get("settled")):
            continue

        key = _row_key(row)
        if not key or key in settled_state:
            continue

        condition_id = (row.get("condition_id") or "").strip()
        side = (row.get("side") or "").strip().upper()
        shares = _safe_float(row.get("shares"))
        price = _safe_float(row.get("price"))
        if not condition_id or side not in {"YES", "NO"} or shares is None or price is None:
            continue

        outcome_payload = None
        try:
            outcome_payload = fetch_market_outcome(condition_id, slug=row.get("event_slug"))
        except Exception:
            outcome_payload = None

        if not isinstance(outcome_payload, dict):
            continue

        outcome = outcome_payload.get("outcome") or outcome_payload.get("winner")
        settled = bool(outcome_payload.get("settled")) if "settled" in outcome_payload else None
        if not settled or not outcome:
            continue

        pnl_usd, pnl_source = _compute_final_pnl_from_cash_pnl(
            wallet=wallet_address,
            condition_id=condition_id,
        )
        if pnl_usd is None:
            pnl_usd = _compute_final_pnl(side=side, shares=shares, price=price, outcome=str(outcome))
            pnl_source = "settlement_only"

        try:
            update_trade_row(
                xlsx_path=xlsx_path,
                key_row={
                    "condition_id": condition_id,
                    "side": side,
                    "recorded_at_utc": row.get("recorded_at_utc"),
                },
                updates={
                    "outcome": str(outcome).strip().upper(),
                    "settled": True,
                    "pnl_usd": pnl_usd,
                    "pnl_source": pnl_source,
                    "final_pnl_usd": pnl_usd,
                },
            )
        except Exception:
            pass

        record = TradeRecord(
            recorded_at_utc=row.get("recorded_at_utc") or "",
            recorded_at=_now_local_hms(),
            run_folder=str(run_folder),
            condition_id=condition_id,
            market_id=row.get("market_id") or None,
            event_slug=row.get("event_slug") or None,
            question=row.get("question") or None,
            window_start_utc=row.get("window_start_utc") or None,
            window_end_utc=row.get("window_end_utc") or None,
            direction=row.get("direction") or None,
            side=side,
            order_type=row.get("order_type") or None,
            post_only=row.get("post_only") if row.get("post_only") not in ("", None) else None,
            shares=shares,
            price=price,
            amount_usd=_safe_float(row.get("amount_usd")),
            order_id=row.get("order_id") or None,
            execution_route=row.get("execution_route") or None,
            mode=row.get("mode") or None,
            outcome=str(outcome).strip().upper(),
            settled=True,
            pnl_usd=pnl_usd,
            pnl_source=pnl_source,
            final_pnl_usd=pnl_usd,
        )
        _append_to_csv(csv_path, [asdict(record)])

        settled_state[key] = {
            "outcome": str(outcome),
            "settled_at": _now_local_hms(),
        }
        processed += 1

    if processed:
        _save_state(run_folder, state)

    return processed


def run_loop(*, run_folder: str | Path, interval_sec: int = 120, quiet: bool = True) -> None:
    """Background loop that reconciles settlements every interval_sec."""
    run_folder = Path(run_folder)
    while True:
        try:
            appended = reconcile_once(run_folder=run_folder)
            if appended and not quiet:
                print(f"[settlement] appended {appended} rows")
        except Exception as exc:
            if not quiet:
                print(f"[settlement] error: {exc}")
        time.sleep(interval_sec)
