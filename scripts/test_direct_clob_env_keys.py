#!/usr/bin/env python3
"""Standalone diagnostic for Polymarket direct CLOB auth/api-key failures.

This script avoids importing project trading code so it can isolate whether a
400 `Could not create api key` error is caused by:
- missing or malformed env values
- private key / wallet address mismatch
- funder/profile mismatch
- unsupported host / chain_id / signature_type combination
- py_clob_client_v2 failing during create_or_derive_api_key()

It only tests CLOB client initialization and API key derivation.
Sensitive values are redacted in output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_env_file as core_load_env_file, resolve_config
from strategy.dual_wallet_models import build_wallet_identities


def load_env_file(project_root: Path) -> Path | None:
    core_load_env_file(str(project_root / "core" / "config.py"))
    env_path = project_root / ".env"
    return env_path if env_path.exists() else None


def resolve_default_private_key_env(project_root: Path) -> str:
    config = resolve_config(str(project_root / "core" / "config.py"))
    return config.get("dual_wallet_wallet_a_private_key_env", "WALLET_PRIVATE_KEY_A")


def resolve_default_wallet_envs(project_root: Path) -> list[str]:
    config = resolve_config(str(project_root / "core" / "config.py"))
    wallets = build_wallet_identities(
        wallet_a_private_key_env=config.get("dual_wallet_wallet_a_private_key_env", "WALLET_PRIVATE_KEY_A"),
        wallet_b_private_key_env=config.get("dual_wallet_wallet_b_private_key_env", "WALLET_PRIVATE_KEY_B"),
    )
    return [wallet.private_key_env for wallet in wallets]


def find_project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists() or (candidate / "requirements.txt").exists():
            return candidate
    return current


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def redact(value: str | None, *, left: int = 6, right: int = 4) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= left + right:
        return "*" * len(text)
    return f"{text[:left]}...{text[-right:]}"


def classify_exception_message(message: str) -> list[str]:
    lower = message.lower()
    hints: list[str] = []
    if "could not create api key" in lower:
        hints.append("polymarket_rejected_api_key_creation")
    if "unauthorized" in lower or "forbidden" in lower:
        hints.append("auth_or_permission_problem")
    if "400" in lower:
        hints.append("http_400_bad_request")
    if "401" in lower or "403" in lower:
        hints.append("http_auth_error")
    if "signature" in lower:
        hints.append("signature_mismatch")
    if "funder" in lower or "proxy" in lower or "profile" in lower:
        hints.append("funder_profile_mismatch")
    if "chain" in lower:
        hints.append("chain_id_mismatch")
    if "invalid" in lower and "private" in lower:
        hints.append("invalid_private_key")
    if "nonce" in lower:
        hints.append("nonce_or_signing_issue")
    return hints


def wallet_address_from_private_key(private_key: str) -> tuple[str | None, str | None]:
    try:
        from eth_account import Account

        return Account.from_key(private_key).address, None
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def collect_env_snapshot(private_key_env: str, funder_env: str, explicit_funder: str | None = None) -> dict[str, Any]:
    return {
        "private_key_env": private_key_env,
        "private_key_present": bool(os.environ.get(private_key_env)),
        "private_key_preview": redact(os.environ.get(private_key_env)),
        "funder_env": funder_env,
        "funder_present": bool(os.environ.get(funder_env)),
        "funder_preview": redact(os.environ.get(funder_env)),
        "explicit_funder": redact(explicit_funder),
        "explicit_funder_present": bool(explicit_funder),
        "POLYMARKET_CLOB_HOST": os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
        "POLYMARKET_CHAIN_ID": os.environ.get("POLYMARKET_CHAIN_ID", "137"),
        "POLYMARKET_SIGNATURE_TYPE": os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"),
    }


def choose_funder(private_key: str, funder_env: str, explicit_funder: str | None = None) -> tuple[str | None, str, str | None]:
    if explicit_funder:
        return explicit_funder, "explicit_arg", None
    configured_funder = os.environ.get(funder_env)
    derived_wallet, derive_error = wallet_address_from_private_key(private_key)
    if configured_funder:
        return configured_funder, "configured_env", derive_error
    if derived_wallet:
        return derived_wallet, "derived_from_private_key", derive_error
    return None, "missing", derive_error


def run_probe(*, private_key_env: str, funder_env: str, explicit_funder: str | None, host: str, chain_id: int, signature_type: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": False,
        "private_key_env": private_key_env,
        "funder_env": funder_env,
        "host": host,
        "chain_id": chain_id,
        "signature_type": signature_type,
    }

    private_key = require_env(private_key_env)
    derived_wallet, derive_error = wallet_address_from_private_key(private_key)
    funder, funder_source, choose_funder_derive_error = choose_funder(private_key, funder_env, explicit_funder)

    result["derived_wallet_address"] = derived_wallet
    result["derived_wallet_error"] = derive_error
    result["funder"] = funder
    result["funder_source"] = funder_source
    result["funder_matches_derived_wallet"] = bool(
        derived_wallet and funder and derived_wallet.lower() == funder.lower()
    )
    if derive_error or choose_funder_derive_error:
        result["identity_diagnostics"] = {
            "derived_wallet_error": derive_error,
            "funder_selection_derive_error": choose_funder_derive_error,
        }
    if derived_wallet is None and derive_error:
        result["error_type"] = "WalletDerivationFailed"
        result["error"] = f"Could not derive wallet address from {private_key_env}: {derive_error}"
        result["hints"] = ["missing_eth_account_dependency_or_invalid_private_key"]
        return result
    if funder is None:
        result["error_type"] = "MissingFunder"
        result["error"] = (
            f"Missing funder/profile address: env {funder_env} is unset and wallet address could not be derived from {private_key_env}"
        )
        result["hints"] = ["missing_funder", "invalid_private_key"]
        return result

    try:
        from py_clob_client_v2 import ClobClient

        result["client_import_ok"] = True
    except Exception as exc:
        result["client_import_ok"] = False
        result["error_type"] = exc.__class__.__name__
        result["error"] = str(exc)
        result["hints"] = ["missing_py_clob_client_v2_dependency"]
        return result

    try:
        client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=private_key,
            signature_type=signature_type,
            funder=funder,
        )
        result["client_init_ok"] = True
        result["client_class"] = client.__class__.__name__
    except Exception as exc:
        message = str(exc)
        result["client_init_ok"] = False
        result["error_type"] = exc.__class__.__name__
        result["error"] = message
        result["hints"] = classify_exception_message(message)
        result["traceback"] = traceback.format_exc()
        return result

    try:
        creds = client.create_or_derive_api_key()
        result["api_key_derivation_ok"] = True
        result["success"] = True
        result["api_key_present"] = bool(getattr(creds, "api_key", None) or getattr(creds, "key", None))
        result["api_secret_present"] = bool(
            getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
        )
        result["api_passphrase_present"] = bool(
            getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None)
        )
        return result
    except Exception as exc:
        message = str(exc)
        result["api_key_derivation_ok"] = False
        result["error_type"] = exc.__class__.__name__
        result["error"] = message
        result["hints"] = classify_exception_message(message)
        result["traceback"] = traceback.format_exc()
        if "could not create api key" in message.lower():
            result["likely_root_causes"] = [
                "private_key_does_not_control_the_expected_polymarket_profile",
                "funder_is_not_the_correct_profile_address_for_this_private_key",
                "wallet_has_not_been properly set up for clob auth",
                "host_chain_signature_combination_is_wrong_for_this_account",
            ]
        return result


def print_human_report(report: dict[str, Any]) -> None:
    print("=== Direct CLOB auth/api-key diagnostic ===")
    print(f"project_root: {report['project_root']}")
    print(f"env_file: {report['env_file']}")

    if report.get("auto_wallet_reports"):
        for index, wallet_report in enumerate(report["auto_wallet_reports"], start=1):
            print(f"wallet_probe_{index}:")
            snapshot = wallet_report["env_snapshot"]
            print(f"  private_key_env: {snapshot['private_key_env']}")
            print(f"  private_key_present: {snapshot['private_key_present']}")
            print(f"  private_key_preview: {snapshot['private_key_preview']}")
            print(f"  funder_env: {snapshot['funder_env']}")
            print(f"  funder_present: {snapshot['funder_present']}")
            print(f"  funder_preview: {snapshot['funder_preview']}")
            print(f"  explicit_funder_present: {snapshot['explicit_funder_present']}")
            print(f"  explicit_funder: {snapshot['explicit_funder']}")
            print(f"  POLYMARKET_CLOB_HOST: {snapshot['POLYMARKET_CLOB_HOST']}")
            print(f"  POLYMARKET_CHAIN_ID: {snapshot['POLYMARKET_CHAIN_ID']}")
            print(f"  POLYMARKET_SIGNATURE_TYPE: {snapshot['POLYMARKET_SIGNATURE_TYPE']}")
            probe = wallet_report["probe"]
            print(f"  client_import_ok: {probe.get('client_import_ok')}")
            print(f"  client_init_ok: {probe.get('client_init_ok')}")
            print(f"  api_key_derivation_ok: {probe.get('api_key_derivation_ok')}")
            print(f"  derived_wallet_address: {probe.get('derived_wallet_address')}")
            if probe.get("derived_wallet_error"):
                print(f"  derived_wallet_error: {probe.get('derived_wallet_error')}")
            print(f"  funder: {probe.get('funder')}")
            print(f"  funder_source: {probe.get('funder_source')}")
            print(f"  funder_matches_derived_wallet: {probe.get('funder_matches_derived_wallet')}")
            if probe.get("success"):
                print("  result: success")
                continue
            print("  result: failure")
            print(f"  error_type: {probe.get('error_type')}")
            print(f"  error: {probe.get('error')}")
            if probe.get("hints"):
                print(f"  hints: {', '.join(probe['hints'])}")
            if probe.get("likely_root_causes"):
                print("  likely_root_causes:")
                for item in probe["likely_root_causes"]:
                    print(f"    - {item}")
            mismatch = probe.get("derived_wallet_address") and probe.get("funder") and not probe.get("funder_matches_derived_wallet")
            if mismatch:
                print("  strong_signal: funder/profile does not match the private key derived wallet address")
        return

    print("env_snapshot:")
    snapshot = report["env_snapshot"]
    print(f"  private_key_env: {snapshot['private_key_env']}")
    print(f"  private_key_present: {snapshot['private_key_present']}")
    print(f"  private_key_preview: {snapshot['private_key_preview']}")
    print(f"  funder_env: {snapshot['funder_env']}")
    print(f"  funder_present: {snapshot['funder_present']}")
    print(f"  funder_preview: {snapshot['funder_preview']}")
    print(f"  explicit_funder_present: {snapshot['explicit_funder_present']}")
    print(f"  explicit_funder: {snapshot['explicit_funder']}")
    print(f"  POLYMARKET_CLOB_HOST: {snapshot['POLYMARKET_CLOB_HOST']}")
    print(f"  POLYMARKET_CHAIN_ID: {snapshot['POLYMARKET_CHAIN_ID']}")
    print(f"  POLYMARKET_SIGNATURE_TYPE: {snapshot['POLYMARKET_SIGNATURE_TYPE']}")

    probe = report["probe"]
    print("probe:")
    print(f"  client_import_ok: {probe.get('client_import_ok')}")
    print(f"  client_init_ok: {probe.get('client_init_ok')}")
    print(f"  api_key_derivation_ok: {probe.get('api_key_derivation_ok')}")
    print(f"  derived_wallet_address: {probe.get('derived_wallet_address')}")
    if probe.get("derived_wallet_error"):
        print(f"  derived_wallet_error: {probe.get('derived_wallet_error')}")
    print(f"  funder: {probe.get('funder')}")
    print(f"  funder_source: {probe.get('funder_source')}")
    print(f"  funder_matches_derived_wallet: {probe.get('funder_matches_derived_wallet')}")
    if probe.get("success"):
        print("  result: success")
        print(f"  api_key_present: {probe.get('api_key_present')}")
        print(f"  api_secret_present: {probe.get('api_secret_present')}")
        print(f"  api_passphrase_present: {probe.get('api_passphrase_present')}")
        return

    print("  result: failure")
    print(f"  error_type: {probe.get('error_type')}")
    print(f"  error: {probe.get('error')}")
    if probe.get("hints"):
        print(f"  hints: {', '.join(probe['hints'])}")
    if probe.get("likely_root_causes"):
        print("  likely_root_causes:")
        for item in probe["likely_root_causes"]:
            print(f"    - {item}")

    mismatch = probe.get("derived_wallet_address") and probe.get("funder") and not probe.get("funder_matches_derived_wallet")
    if mismatch:
        print("  strong_signal: funder/profile does not match the private key derived wallet address")


def auto_probe_wallets(
    project_root: Path,
    funder_env: str,
    explicit_funder: str | None,
    host: str,
    chain_id: int,
    signature_type: int,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for private_key_env in resolve_default_wallet_envs(project_root):
        reports.append(
            {
                "env_snapshot": collect_env_snapshot(private_key_env, funder_env, explicit_funder),
                "probe": run_probe(
                    private_key_env=private_key_env,
                    funder_env=funder_env,
                    explicit_funder=explicit_funder,
                    host=host,
                    chain_id=chain_id,
                    signature_type=signature_type,
                ),
            }
        )
    return reports


def main() -> int:
    parser = argparse.ArgumentParser(description="Direct Polymarket CLOB auth/api-key diagnostic")
    parser.add_argument("--project-root", default=".", help="Project root containing .env")
    parser.add_argument("--private-key-env", default=None, help="Env var containing private key; defaults to wallet A config env")
    parser.add_argument("--all-wallets", action="store_true", help="Probe both configured wallet envs instead of a single wallet")
    parser.add_argument("--funder-env", default="POLYMARKET_FUNDER", help="Env var containing funder/profile address")
    parser.add_argument("--funder", default=None, help="Explicit funder/profile address to test instead of env or derived wallet")
    parser.add_argument("--host", default=None, help="Override CLOB host")
    parser.add_argument("--chain-id", type=int, default=None, help="Override chain id")
    parser.add_argument("--signature-type", type=int, default=None, help="Override signature type")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    project_root = find_project_root(Path(args.project_root))
    env_path = load_env_file(project_root)
    if env_path is None:
        raise RuntimeError(f"No .env file found under {project_root}")

    host = args.host or os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
    chain_id = args.chain_id or int(os.environ.get("POLYMARKET_CHAIN_ID", "137"))
    signature_type = args.signature_type or int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "1"))

    report: dict[str, Any] = {
        "project_root": str(project_root),
        "env_file": str(env_path),
    }

    if args.all_wallets:
        report["auto_wallet_reports"] = auto_probe_wallets(
            project_root=project_root,
            funder_env=args.funder_env,
            explicit_funder=args.funder,
            host=host,
            chain_id=chain_id,
            signature_type=signature_type,
        )
    else:
        private_key_env = args.private_key_env or resolve_default_private_key_env(project_root)
        report["env_snapshot"] = collect_env_snapshot(private_key_env, args.funder_env, args.funder)
        report["probe"] = run_probe(
            private_key_env=private_key_env,
            funder_env=args.funder_env,
            explicit_funder=args.funder,
            host=host,
            chain_id=chain_id,
            signature_type=signature_type,
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human_report(report)

    if report.get("auto_wallet_reports"):
        return 0 if all(item["probe"].get("success") for item in report["auto_wallet_reports"]) else 1
    return 0 if report["probe"].get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
