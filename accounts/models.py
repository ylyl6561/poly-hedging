from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccountSpec:
    account_id: str
    label: str
    private_key_env: str
    funder_env: str | None = None
    proxy_address_env: str | None = None
    signature_type: int | None = None
    enabled: bool = True
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccountContext:
    account_id: str
    label: str
    private_key_env: str
    funder_env: str | None
    proxy_address_env: str | None
    private_key: str
    wallet_address: str
    funder_address: str | None
    proxy_address: str | None
    host: str
    chain_id: int
    signature_type: int
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClientCacheKey:
    account_id: str
    wallet_address: str
    funder_address: str | None
    proxy_address: str | None
    host: str
    chain_id: int
    signature_type: int


@dataclass
class ClientRecord:
    client: object
    cache_key: ClientCacheKey
    created_at: float
    last_used_at: float
