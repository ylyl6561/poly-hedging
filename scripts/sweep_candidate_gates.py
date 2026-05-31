#!/usr/bin/env python3
"""One-variable-at-a-time gate sweep for FastLoop candidate journals."""

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path


BASELINE = {
    "min_time_remaining": 45,
    "trend_confirm_min_distance_pct": 0.030,
    "trend_confirm_min_momentum_pct": 0.010,
    "trend_confirm_require_macro_alignment": True,
    "trend_confirm_macro_min_abs_pct": 0.050,
    "chop_breakout_distance_pct": 0.080,
    "window_path_min_score": 70,
    "window_path_post_flip_breakout_pct": 0.120,
    "window_path_min_progress_ratio": 0.700,
    "trend_confirm_max_entry_price": 0.930,
    "trend_chase_min_entry_price": 0.500,
    "trend_chase_min_distance_pct": 0.080,
    "trend_chase_max_negative_edge": 0.100,
    "ai_low_volume_min_ratio": 0.100,
    "ai_low_volume_override_min_edge": 0.040,
    "ai_midprice_min_edge": 0.020,
    "ai_midprice_max_distance_pct": 0.200,
}


SWEEPS = {
    "min_time_remaining": [15, 30, 45, 60],
    "trend_confirm_min_distance_pct": [0.020, 0.030, 0.050],
    "trend_confirm_min_momentum_pct": [0.003, 0.010, 0.020, 0.050],
    "trend_confirm_require_macro_alignment": [False, True],
    "trend_confirm_macro_min_abs_pct": [0.030, 0.050, 0.080, 0.120],
    "chop_breakout_distance_pct": [0.030, 0.050, 0.080, 0.100],
    "window_path_min_score": [50, 60, 70, 80],
    "window_path_post_flip_breakout_pct": [0.080, 0.100, 0.120, 0.150],
    "window_path_min_progress_ratio": [0.500, 0.700, 0.850],
}


def load_records(path):
    records = []
    with open(path) as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"skip malformed JSONL line {line_no}: {exc}", file=sys.stderr)
    return records


def as_float(record, key, default=0.0):
    try:
        return float(record.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def as_int(record, key, default=0):
    try:
        return int(record.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def macro_blocks(record, cfg):
    if not cfg["trend_confirm_require_macro_alignment"]:
        return False
    macro_pct = record.get("macro_momentum_pct")
    if macro_pct is None:
        return True
    macro_material = abs(as_float(record, "macro_momentum_pct")) >= cfg["trend_confirm_macro_min_abs_pct"]
    return macro_material and not bool(record.get("macro_momentum_aligned"))


def evaluate(record, cfg):
    blocks = []
    remaining = record.get("remaining_sec")
    if remaining is not None and as_float(record, "remaining_sec") <= cfg["min_time_remaining"]:
        blocks.append("time")

    distance = abs(as_float(record, "distance_from_open_pct"))
    if distance < cfg["trend_confirm_min_distance_pct"]:
        blocks.append("distance")

    if abs(as_float(record, "momentum_pct")) < cfg["trend_confirm_min_momentum_pct"]:
        blocks.append("recent_momentum_strength")

    if not bool(record.get("recent_momentum_aligned", True)):
        blocks.append("recent_momentum_alignment")

    if macro_blocks(record, cfg):
        blocks.append("macro_alignment")

    ai_regime = record.get("ai_regime")
    if ai_regime in {"base", "low-volume-aligned"} and distance < cfg["chop_breakout_distance_pct"]:
        blocks.append("breakout_distance")

    sign_flips = as_int(record, "market_quality_sign_flips")
    if sign_flips and distance < cfg["window_path_post_flip_breakout_pct"]:
        blocks.append("post_flip_breakout")

    progress_ratio = as_float(record, "window_path_progress_ratio", 1.0)
    if progress_ratio < cfg["window_path_min_progress_ratio"]:
        blocks.append("path_progress")

    score = record.get("window_path_score")
    if score is not None and as_float(record, "window_path_score") < cfg["window_path_min_score"]:
        blocks.append("path_score")

    entry_price = as_float(record, "entry_price")
    selected_edge = as_float(record, "selected_edge")
    volume_ratio = as_float(record, "volume_ratio", 1.0)
    recent_aligned = bool(record.get("recent_momentum_aligned", True))
    if entry_price > cfg["trend_confirm_max_entry_price"]:
        blocks.append("entry_price")

    trend_chase_allowed = (
        entry_price >= cfg["trend_chase_min_entry_price"]
        and distance >= cfg["trend_chase_min_distance_pct"]
        and selected_edge >= -cfg["trend_chase_max_negative_edge"]
        and volume_ratio >= cfg["ai_low_volume_min_ratio"]
        and recent_aligned
    )
    dynamic_min_edge = as_float(record, "dynamic_min_edge")
    if selected_edge < dynamic_min_edge and not trend_chase_allowed:
        blocks.append("edge")

    midprice_entry = 0.35 <= entry_price <= 0.75
    if midprice_entry and selected_edge < cfg["ai_midprice_min_edge"] and not trend_chase_allowed:
        blocks.append("midprice_edge")
    if midprice_entry and distance > cfg["ai_midprice_max_distance_pct"]:
        blocks.append("midprice_distance")

    low_volume_override = (
        volume_ratio < 0.5
        and volume_ratio >= cfg["ai_low_volume_min_ratio"]
        and selected_edge >= cfg["ai_low_volume_override_min_edge"]
    ) or trend_chase_allowed
    if volume_ratio < 0.5 and not low_volume_override:
        blocks.append("low_volume_final")

    return blocks


def summarize(records, cfg):
    first_blocks = Counter()
    any_blocks = Counter()
    allowed = 0
    for record in records:
        blocks = evaluate(record, cfg)
        if not blocks:
            allowed += 1
            continue
        first_blocks[blocks[0]] += 1
        any_blocks.update(blocks)
    return allowed, first_blocks, any_blocks


def print_row(name, value, records, cfg, baseline_allowed):
    allowed, first_blocks, any_blocks = summarize(records, cfg)
    delta = allowed - baseline_allowed
    first = first_blocks.most_common(3)
    any_top = any_blocks.most_common(3)
    first_text = ", ".join(f"{k}:{v}" for k, v in first) or "-"
    any_text = ", ".join(f"{k}:{v}" for k, v in any_top) or "-"
    print(
        f"{name:42} {str(value):>8} {allowed:7d} {delta:+7d} "
        f"{first_text:34} {any_text}"
    )


def main():
    parser = argparse.ArgumentParser(description="Sweep one FastLoop gate at a time")
    parser.add_argument("journal", help="Path to candidate_journal.jsonl")
    parser.add_argument("--param", choices=sorted(SWEEPS), help="Only sweep one parameter")
    parser.add_argument("--values", help="Comma-separated custom values for --param")
    parser.add_argument("--base", action="append", default=[],
                        help="Override baseline as name=value; can be repeated")
    args = parser.parse_args()

    path = Path(args.journal).expanduser()
    if not path.exists():
        raise SystemExit(f"journal not found: {path}")

    records = load_records(path)
    baseline = deepcopy(BASELINE)
    for item in args.base:
        if "=" not in item:
            raise SystemExit(f"bad --base override: {item}")
        name, raw_value = item.split("=", 1)
        if name not in baseline:
            raise SystemExit(f"unknown --base parameter: {name}")
        if isinstance(baseline[name], bool):
            baseline[name] = raw_value.lower() in {"1", "true", "yes", "on"}
        elif isinstance(baseline[name], int):
            baseline[name] = int(raw_value)
        else:
            baseline[name] = float(raw_value)

    baseline_allowed, baseline_first, baseline_any = summarize(records, baseline)
    print(f"Loaded rows: {len(records)}")
    print(f"Baseline allowed: {baseline_allowed}")
    print("Baseline first blocks: " + (", ".join(f"{k}:{v}" for k, v in baseline_first.most_common()) or "-"))
    print("Baseline any blocks: " + (", ".join(f"{k}:{v}" for k, v in baseline_any.most_common()) or "-"))
    print()
    print(f"{'param':42} {'value':>8} {'allowed':>7} {'delta':>7} {'top first blocks':34} top any blocks")
    print("-" * 118)

    sweep_items = [(args.param, SWEEPS[args.param])] if args.param else SWEEPS.items()
    for name, values in sweep_items:
        if args.values:
            if not args.param:
                raise SystemExit("--values requires --param")
            raw_values = [item.strip() for item in args.values.split(",") if item.strip()]
            if isinstance(BASELINE[name], bool):
                values = [item.lower() in {"1", "true", "yes", "on"} for item in raw_values]
            elif isinstance(BASELINE[name], int):
                values = [int(item) for item in raw_values]
            else:
                values = [float(item) for item in raw_values]

        for value in values:
            cfg = deepcopy(baseline)
            cfg[name] = value
            print_row(name, value, records, cfg, baseline_allowed)


if __name__ == "__main__":
    main()
