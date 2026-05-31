"""Trade reconciliation export (CSV + optional Excel) for FastLoop runs.

Writes an append-only trades.csv (always) and trades.xlsx (if openpyxl exists)
into the current run folder.

Data sources:
- Order intents & execution metadata are captured at trade time.
- Outcome comes from Polymarket CLOB/Gamma lookup (api.fetch_market_outcome).
- PnL comes from Polymarket Data API (public) when available.

This module is best-effort: it never blocks trading if export fails.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import csv

from api.api import api_request, fetch_market_outcome


DATA_API = "https://data-api.polymarket.com"


@dataclass
class TradeRecord:
    recorded_at_utc: str
    recorded_at: str
    run_folder: str

    condition_id: str | None
    market_id: str | None
    event_slug: str | None
    question: str | None
    window_start_utc: str | None
    window_end_utc: str | None

    direction: str  # UP or DOWN
    side: str  # YES or NO
    order_type: str | None
    post_only: bool | None

    shares: float | None
    price: float | None
    amount_usd: float | None

    order_id: str | None
    execution_route: str | None
    mode: str | None  # live/paper

    outcome: str | None
    settled: bool | None
    pnl_usd: float | None
    pnl_source: str | None

    final_pnl_usd: float | None


def _iso_now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_now_hms() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _append_to_csv(path: Path, rows: list[dict[str, Any]]):
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = list(rows[0].keys())
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        if not file_exists:
            writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _append_to_xlsx(path: Path, rows: list[dict[str, Any]]):
    # Optional dependency
    try:
        from openpyxl import Workbook, load_workbook
    except Exception:
        return

    headers = list(rows[0].keys()) if rows else []
    if not headers:
        return

    if path.exists():
        wb = load_workbook(path)
        ws = wb.active
        existing = [cell.value for cell in ws[1]] if ws.max_row >= 1 else []
        if existing:
            headers = existing
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = "trades"
        ws.append(headers)

    ws = wb.active
    for row in rows:
        ws.append([row.get(h) for h in headers])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _data_api_positions(user: str, market: str | None = None) -> list[dict[str, Any]]:
    if not user:
        return []
    url = f"{DATA_API}/positions?user={user}"
    if market:
        url += f"&market={market}"
    payload = api_request(url, timeout=10)
    if not payload or isinstance(payload, dict) and payload.get("error"):
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return []


def _compute_pnl_from_positions(
    *,
    wallet: str,
    condition_id: str,
    side: str,
    shares: float | None,
    price: float | None,
) -> tuple[float | None, str | None]:
    if not wallet or not condition_id:
        return None, None

    positions = _data_api_positions(wallet, market=condition_id)
    if not positions:
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


def export_preopen_action_to_excel(
    *,
    run_folder: str | Path,
    event: Any,
    action: Any,
    dry_run: bool,
    execution_route: str | None = None,
    wallet_address: str | None = None,
):
    run_folder = Path(run_folder)
    csv_path = run_folder / "trades.csv"
    xlsx_path = run_folder / "trades.xlsx"

    action_name = getattr(action, "action", None)
    if action_name == "yes_entry":
        side = "YES"
        post_only = False
        order_type = "GTC"
    elif action_name == "down_resting":
        side = "NO"
        post_only = True
        order_type = "GTC"
    elif action_name == "down_switch":
        side = "NO"
        post_only = False
        order_type = "FAK"
    else:
        return

    direction = "UP" if side == "YES" else "DOWN"

    shares = _safe_float(getattr(action, "shares", None))
    price = _safe_float(getattr(action, "price", None))

    # Some action paths may not populate order_id (especially in dry-run);
    # we still want the CSV row as long as shares/price exist.
    order_id = getattr(action, "order_id", None)

    amount_usd = None
    if shares is not None and price is not None:
        amount_usd = round(shares * price, 6)

    condition_id = getattr(event, "condition_id", None)
    event_slug = getattr(event, "slug", None)

    outcome = None
    settled = None
    try:
        outcome_payload = fetch_market_outcome(
            condition_id,
            slug=event_slug,
            clob_token_ids=getattr(event, "clob_token_ids", None),
        )
        if isinstance(outcome_payload, dict):
            outcome = outcome_payload.get("outcome") or outcome_payload.get("winner")
            settled = bool(outcome_payload.get("settled")) if "settled" in outcome_payload else None
    except Exception:
        pass

    pnl_usd = None
    pnl_source = None
    try:
        pnl_usd, pnl_source = _compute_pnl_from_positions(
            wallet=str(wallet_address or ""),
            condition_id=str(condition_id or ""),
            side=str(side or ""),
            shares=shares,
            price=price,
        )
    except Exception:
        pnl_usd, pnl_source = None, None

    if pnl_usd is None and settled and outcome and shares is not None and price is not None and side:
        win_side = str(outcome).strip().upper()
        payout = shares if win_side == side else 0.0
        cost = shares * price
        pnl_usd = payout - cost
        pnl_source = "settlement_only"

    final_pnl_usd = _safe_float(pnl_usd) if settled and pnl_usd is not None else None

    record = TradeRecord(
        recorded_at_utc=_iso_now_utc(),
        recorded_at=_local_now_hms(),
        run_folder=str(run_folder),
        condition_id=str(condition_id) if condition_id else None,
        market_id=None,
        event_slug=str(event_slug) if event_slug else None,
        question=getattr(event, "question", None),
        window_start_utc=getattr(event, "start_time", None).isoformat() if getattr(event, "start_time", None) else None,
        window_end_utc=getattr(event, "end_time", None).isoformat() if getattr(event, "end_time", None) else None,
        direction=direction,
        side=side,
        order_type=order_type,
        post_only=post_only,
        shares=shares,
        price=price,
        amount_usd=amount_usd,
        order_id=str(order_id) if order_id else None,
        execution_route=execution_route,
        mode="paper" if dry_run else "live",
        outcome=str(outcome).upper() if outcome else None,
        settled=settled,
        pnl_usd=_safe_float(pnl_usd),
        pnl_source=pnl_source,
        final_pnl_usd=final_pnl_usd,
    )

    row = asdict(record)
    _append_to_csv(csv_path, [row])
    _append_to_xlsx(xlsx_path, [row])
