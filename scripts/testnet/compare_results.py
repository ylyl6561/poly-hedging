"""
compare_results.py
==================

Side-by-side comparison table for all `scenario_result.json` files matching
a glob pattern. Useful for sanity-checking the fake-amoy suite, replay runs,
or back-to-back dry-runs against a baseline.

Usage
-----
    .venv/bin/python scripts/testnet/compare_results.py \
        --pattern "main/runs/fake_amoy_*/scenario_result.json"

    # Fail with exit code 1 if any row diverges from its expected fields:
    .venv/bin/python scripts/testnet/compare_results.py --strict
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare scenario_result.json files")
    parser.add_argument("--pattern", required=True, help="Glob pattern for scenario_result.json files")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero on any expected-field divergence")
    args = parser.parse_args()

    files = sorted(glob.glob(args.pattern))
    if not files:
        print(f"No files matched: {args.pattern}", file=sys.stderr)
        return 1

    rows = []
    for path in files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"⚠ Skipping {path}: {exc}")
            continue
        rows.append((Path(path).parent.name, data))

    # Header
    headers = ["scenario", "outcome", "filled", "cancelled", "force_closed", "total_pnl", "wallet_a_pnl", "wallet_b_pnl", "is_profit", "expected"]
    col_widths = [max(28, max(len(r[0]) for r in rows) + 2)] + [10] * (len(headers) - 2) + [20]
    print(" | ".join(h.ljust(w) for h, w in zip(headers, col_widths)))
    print("-+-".join("-" * w for w in col_widths))

    mismatches = 0
    for name, data in rows:
        wallet_pnl = data.get("wallet_pnl_usd", {}) or {}
        wa = wallet_pnl.get("wallet_a", 0.0)
        wb = wallet_pnl.get("wallet_b", 0.0)
        expected = f"trig={data.get('expected_trigger_reason', '?')} fill={data.get('expected_filled_count', '?')}"
        cells = [
            name,
            str(data.get("outcome", "?")),
            str(data.get("filled_count", 0)),
            str(data.get("cancelled_count", 0)),
            str(data.get("force_closed_count", 0)),
            f"{data.get('total_pnl_usd', 0.0):.4f}",
            f"{wa:.4f}",
            f"{wb:.4f}",
            str(data.get("is_profit", False)),
            expected,
        ]
        print(" | ".join(c.ljust(w) for c, w in zip(cells, col_widths)))

        if args.strict:
            # Compare against the run's expected fields by looking at the
            # structured_output.json for the actual trigger reason.
            structured_path = Path(path).parent / "structured_output.json"
            if structured_path.exists():
                structured = json.loads(structured_path.read_text(encoding="utf-8"))
                actual_trigger = None
                for ev in structured.get("events", []):
                    payload = ev.get("payload", {}) or {}
                    note = payload.get("note", "") or ""
                    text = json.dumps(payload, ensure_ascii=False) + " " + note
                    for kw in ("single_side_fill_pending_close", "both_sides_filled", "force_close_window", "wait_timeout_no_clear_fill", "event_already_ended"):
                        if kw in text:
                            actual_trigger = kw
                            break
                    if actual_trigger:
                        break
                exp_trigger = data.get("expected_trigger_reason")
                if exp_trigger and exp_trigger != actual_trigger:
                    print(f"  ⚠ {name}: trigger mismatch (expected={exp_trigger}, actual={actual_trigger})")
                    mismatches += 1
                exp_filled = data.get("expected_filled_count")
                if exp_filled is not None and data.get("filled_count", -1) != exp_filled:
                    print(f"  ⚠ {name}: filled_count mismatch (expected={exp_filled}, actual={data.get('filled_count')})")
                    mismatches += 1

    if args.strict and mismatches:
        print(f"\n{mismatches} mismatch(es) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
