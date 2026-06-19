#!/usr/bin/env python3
"""
test_clob_client_accounts.py

对 config.json 里配置的每个账号，走项目真实的 ClobClientManager 链路，
验证账户凭据是否正确、API key 派生是否成功、余额查询是否可用。

用法：
    python scripts/test_clob_client_accounts.py
    python scripts/test_clob_client_accounts.py --account wallet_a
    python scripts/test_clob_client_accounts.py --json

不走任何真实下单，只做读操作（API key 派生、余额查询）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from accounts import get_account_registry
from api import get_direct_clob_client, get_wallet_usdc_balance, reset_direct_clob_client
from core.config import load_env_file as core_load_env_file

core_load_env_file(str(Path(__file__).resolve().parent / "core" / "config.py"))


def redact(value: str | None, *, left: int = 6, right: int = 4) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= left + right:
        return "*" * len(text)
    return f"{text[:left]}...{text[-right:]}"


def derive_wallet_address(private_key: str) -> tuple[str | None, str | None]:
    try:
        from eth_account import Account
        return Account.from_key(private_key).address, None
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def probe_account(account_id: str, account) -> dict:
    result: dict = {
        "account_id": account_id,
        "label": account.label,
        "wallet_address": account.wallet_address,
        "funder_address": account.funder_address,
        "proxy_address": account.proxy_address,
        "host": account.host,
        "chain_id": account.chain_id,
        "signature_type": account.signature_type,
        "private_key_env": account.private_key_env,
        "funder_env": account.funder_env,
        "proxy_address_env": account.proxy_address_env,
        "steps": {},
    }

    # Step 1: private key 解析
    try:
        derived, err = derive_wallet_address(account.private_key)
        result["steps"]["private_key_derivation"] = {
            "ok": err is None,
            "derived_address": derived,
            "error": err,
        }
        if err:
            result["steps"]["api_key_derivation"] = {"ok": False, "error": "skipped due to private key error"}
            result["steps"]["balance_fetch"] = {"ok": False, "error": "skipped due to private key error"}
            result["summary"] = "FAIL: private_key_unreadable"
            return result
        if derived and derived.lower() != account.wallet_address.lower():
            result["steps"]["private_key_derivation"]["warning"] = (
                f"derived address {derived} != configured wallet_address {account.wallet_address}"
            )
    except Exception as exc:
        result["steps"]["private_key_derivation"] = {"ok": False, "error": str(exc)}
        result["steps"]["api_key_derivation"] = {"ok": False, "error": "skipped"}
        result["steps"]["balance_fetch"] = {"ok": False, "error": "skipped"}
        result["summary"] = "FAIL: private_key_exception"
        return result

    # Step 2: ClobClientManager.get_client — 触发 create_or_derive_api_key
    try:
        reset_direct_clob_client(account=account)
        client = get_direct_clob_client(account=account)
        result["steps"]["client_init"] = {"ok": True, "client_class": client.__class__.__name__}
    except Exception as exc:
        tb = traceback.format_exc()
        result["steps"]["client_init"] = {
            "ok": False,
            "error": str(exc),
            "traceback": tb,
        }
        result["steps"]["api_key_derivation"] = {"ok": False, "error": "skipped: client init failed"}
        result["steps"]["balance_fetch"] = {"ok": False, "error": "skipped"}
        result["summary"] = "FAIL: client_init_error"
        return result

    # Step 3: API key 是否成功派生（create_or_derive_api_key 在 get_client 内部已调用）
    # 通过检查 client._api_creds 或 getattr 探测
    try:
        creds = getattr(client, "_api_creds", None) or getattr(client, "creds", None)
        has_api_key = creds is not None and bool(getattr(creds, "api_key", None) or getattr(creds, "key", None))
        result["steps"]["api_key_derivation"] = {
            "ok": has_api_key,
            "has_api_key": has_api_key,
            "creds_type": type(creds).__name__ if creds else None,
            "creds_repr": repr(creds)[:200] if creds else None,
        }
        if not has_api_key:
            result["steps"]["balance_fetch"] = {"ok": False, "error": "skipped: no api key"}
            result["summary"] = "FAIL: no_api_key"
            return result
    except Exception as exc:
        result["steps"]["api_key_derivation"] = {"ok": False, "error": str(exc)}
        result["steps"]["balance_fetch"] = {"ok": False, "error": "skipped"}
        result["summary"] = "FAIL: api_key_probe_error"
        return result

    # Step 4: 余额查询
    try:
        balance_payload = get_wallet_usdc_balance(account=account)
        ok = isinstance(balance_payload, dict) and balance_payload.get("success") is True
        result["steps"]["balance_fetch"] = {
            "ok": ok,
            "balance_usdc": balance_payload.get("balance_usdc") if ok else None,
            "error": balance_payload.get("error") if not ok else None,
            "raw_success": balance_payload.get("success"),
        }
        if ok:
            result["summary"] = "PASS"
        else:
            result["summary"] = "PARTIAL: client_ok_but_balance_failed"
    except Exception as exc:
        result["steps"]["balance_fetch"] = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
        result["summary"] = "PARTIAL: client_ok_but_balance_error"

    return result


def print_report(reports: list[dict], *, json_only: bool = False) -> None:
    if json_only:
        print(json.dumps(reports, indent=2, default=str))
        return

    print()
    print("=" * 70)
    print(" ClobClientManager Account Diagnostic Report")
    print("=" * 70)

    all_pass = True
    for r in reports:
        summary = r.get("summary", "unknown")
        icon = "✓" if summary == "PASS" else "✗" if summary.startswith("FAIL") else "⚠"
        print(f"\n{icon} [{r['account_id']}] {r['label']}")
        print(f"  wallet_address : {r['wallet_address']}")
        print(f"  signature_type : {r['signature_type']}  |  chain_id : {r['chain_id']}")
        print(f"  host           : {r['host']}")
        print(f"  private_key_env: {r['private_key_env']}")
        print(f"  funder_env     : {r['funder_env']}")
        print(f"  funder_address : {r['funder_address']}")
        print(f"  proxy_address  : {r['proxy_address']}")

        steps = r.get("steps", {})
        for step_name, step_result in steps.items():
            ok = step_result.get("ok", False)
            icon2 = "✓" if ok else "✗"
            error = step_result.get("error")
            print(f"  {icon2} {step_name}: {'OK' if ok else 'FAIL'}" + (f" — {error}" if error else ""))

        print(f"  --> {summary}")
        if not summary.startswith("PASS"):
            all_pass = False

    print()
    print("=" * 70)
    if all_pass:
        print("All accounts passed.")
    else:
        print("One or more accounts have issues — see above for details.")
        print("Run with --json for machine-readable output.")
    print("=" * 70)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test ClobClientManager account configuration")
    parser.add_argument("--account", default=None, help="Test only this account_id (e.g. wallet_a)")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    try:
        registry = get_account_registry()
    except Exception as exc:
        print(f"ERROR: Could not load AccountRegistry: {exc}", file=sys.stderr)
        print("Check that polymarket_accounts is configured in config.json and env vars are set.", file=sys.stderr)
        return 1

    accounts = registry.list_accounts()
    if not accounts:
        print("ERROR: No accounts found in registry. Check config.json polymarket_accounts.", file=sys.stderr)
        return 1

    if args.account:
        accounts = [a for a in accounts if a.account_id == args.account]
        if not accounts:
            print(f"ERROR: Account '{args.account}' not found in registry.", file=sys.stderr)
            print(f"Available accounts: {[a.account_id for a in registry.list_accounts()]}", file=sys.stderr)
            return 1

    reports = []
    for account in accounts:
        print(f"Probing {account.account_id} ({account.label})...", flush=True)
        try:
            report = probe_account(account.account_id, account)
        except Exception as exc:
            report = {
                "account_id": account.account_id,
                "label": account.label,
                "summary": f"FAIL: unhandled_exception: {exc}",
                "traceback": traceback.format_exc(),
            }
        reports.append(report)

    print_report(reports, json_only=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
