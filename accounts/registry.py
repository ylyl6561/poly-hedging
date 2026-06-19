from __future__ import annotations

import os
from dataclasses import asdict

from eth_account import Account

import core.config as core_config

from .models import AccountContext, AccountSpec


def _normalize_tags(raw_tags) -> tuple[str, ...]:
    if isinstance(raw_tags, (list, tuple)):
        return tuple(str(tag).strip() for tag in raw_tags if str(tag).strip())
    if isinstance(raw_tags, str) and raw_tags.strip():
        return tuple(tag.strip() for tag in raw_tags.split(",") if tag.strip())
    return ()


def _default_funder_env(private_key_env: str) -> str | None:
    suffix = private_key_env.removeprefix("WALLET_PRIVATE_KEY")
    if suffix:
        return f"POLYMARKET_FUNDER{suffix}"
    return "POLYMARKET_FUNDER"


def _get_polymarket_accounts_config() -> list[dict]:
    configured_accounts = getattr(core_config, "POLYMARKET_ACCOUNTS", [])
    if not isinstance(configured_accounts, list) or not configured_accounts:
        raise ValueError("polymarket_accounts must be configured for account-aware trading")
    return configured_accounts


def _load_specs() -> list[AccountSpec]:
    configured_accounts = _get_polymarket_accounts_config()

    specs: list[AccountSpec] = []
    for index, item in enumerate(configured_accounts):
        if not isinstance(item, dict):
            raise ValueError(f"polymarket_accounts[{index}] must be an object")
        account_id = str(item.get("account_id") or "").strip()
        private_key_env = str(item.get("private_key_env") or "").strip()
        if not account_id:
            raise ValueError(f"polymarket_accounts[{index}].account_id is required")
        if not private_key_env:
            raise ValueError(f"polymarket_accounts[{index}].private_key_env is required")
        funder_env = item.get("funder_env")
        proxy_address_env = item.get("proxy_address_env")
        signature_type = item.get("signature_type")
        specs.append(
            AccountSpec(
                account_id=account_id,
                label=str(item.get("label") or account_id),
                private_key_env=private_key_env,
                funder_env=str(funder_env).strip() if funder_env else _default_funder_env(private_key_env),
                proxy_address_env=str(proxy_address_env).strip() if proxy_address_env else None,
                signature_type=int(signature_type) if signature_type is not None else None,
                enabled=bool(item.get("enabled", True)),
                tags=_normalize_tags(item.get("tags")),
            )
        )
    return specs


class AccountRegistry:
    def __init__(self):
        self._contexts: dict[str, AccountContext] = {}
        self._load()

    def _load(self) -> None:
        seen_ids: set[str] = set()
        seen_wallets: dict[str, str] = {}
        for spec in _load_specs():
            if not spec.enabled:
                continue
            if spec.account_id in seen_ids:
                raise ValueError(f"duplicate account_id: {spec.account_id}")
            ctx = self._build_context(spec)
            normalized_wallet = ctx.wallet_address.lower()
            if normalized_wallet in seen_wallets:
                raise ValueError(
                    f"duplicate wallet address for accounts {seen_wallets[normalized_wallet]} and {ctx.account_id}: {ctx.wallet_address}"
                )
            seen_ids.add(spec.account_id)
            seen_wallets[normalized_wallet] = ctx.account_id
            self._contexts[ctx.account_id] = ctx

    def _build_context(self, spec: AccountSpec) -> AccountContext:
        private_key = os.environ.get(spec.private_key_env)
        if not private_key:
            raise ValueError(f"missing private key env for account {spec.account_id}: {spec.private_key_env}")
        try:
            wallet_address = Account.from_key(private_key).address
        except Exception as exc:
            raise ValueError(f"invalid private key for account {spec.account_id}: {spec.private_key_env}") from exc
        funder_address = os.environ.get(spec.funder_env) if spec.funder_env else None
        proxy_address = os.environ.get(spec.proxy_address_env) if spec.proxy_address_env else None
        return AccountContext(
            account_id=spec.account_id,
            label=spec.label,
            private_key_env=spec.private_key_env,
            funder_env=spec.funder_env,
            proxy_address_env=spec.proxy_address_env,
            private_key=private_key,
            wallet_address=wallet_address,
            funder_address=funder_address,
            proxy_address=proxy_address,
            host=core_config.DIRECT_CLOB_HOST,
            chain_id=core_config.DIRECT_CLOB_CHAIN_ID,
            signature_type=spec.signature_type if spec.signature_type is not None else core_config.DIRECT_CLOB_SIGNATURE_TYPE,
            tags=spec.tags,
        )

    def list_accounts(self) -> list[AccountContext]:
        return list(self._contexts.values())

    def get(self, account_id: str) -> AccountContext:
        try:
            return self._contexts[account_id]
        except KeyError as exc:
            raise KeyError(f"unknown account_id: {account_id}") from exc

    def accounts_with_tag(self, tag: str) -> list[AccountContext]:
        return [context for context in self._contexts.values() if tag in context.tags]

    def describe_accounts(self) -> list[dict[str, object]]:
        descriptions: list[dict[str, object]] = []
        for context in self.list_accounts():
            payload = asdict(context)
            payload["private_key"] = "***"
            descriptions.append(payload)
        return descriptions


_registry: AccountRegistry | None = None


def get_account_registry() -> AccountRegistry:
    global _registry
    if _registry is None:
        _registry = AccountRegistry()
    return _registry
