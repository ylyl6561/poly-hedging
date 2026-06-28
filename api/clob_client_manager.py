from __future__ import annotations

import threading
import time

from accounts import AccountContext, ClientCacheKey, ClientRecord

from .stderr_utils import call_with_optional_stderr_suppression


class ClobClientManager:
    def __init__(self):
        self._records: dict[ClientCacheKey, ClientRecord] = {}
        self._lock = threading.Lock()

    def _build_cache_key(self, account: AccountContext) -> ClientCacheKey:
        return ClientCacheKey(
            account_id=account.account_id,
            wallet_address=account.wallet_address.lower(),
            funder_address=account.funder_address.lower() if account.funder_address else None,
            proxy_address=account.proxy_address.lower() if account.proxy_address else None,
            host=account.host,
            chain_id=account.chain_id,
            signature_type=account.signature_type,
        )

    def get_client(self, account: AccountContext):
        cache_key = self._build_cache_key(account)

        # 快速路径：已缓存则无需加锁（dict + GIL 保证原子读）
        record = self._records.get(cache_key)
        if record is not None:
            record.last_used_at = time.time()
            return record.client

        # 慢路径：首次创建需要加锁，防止同一账户被并发重复初始化
        with self._lock:
            # Double-check：其他线程可能已在锁外完成了初始化
            record = self._records.get(cache_key)
            if record is not None:
                record.last_used_at = time.time()
                return record.client

            try:
                from py_clob_client_v2 import ClobClient
            except ImportError as exc:
                raise RuntimeError("py-clob-client-v2 is required for direct Polymarket CLOB trading") from exc

            client = ClobClient(
                account.host,
                key=account.private_key,
                chain_id=account.chain_id,
                signature_type=account.signature_type,
                funder=account.funder_address,
            )
            creds = call_with_optional_stderr_suppression(client.create_or_derive_api_key)
            client.set_api_creds(creds)
            now = time.time()
            self._records[cache_key] = ClientRecord(
                client=client,
                cache_key=cache_key,
                created_at=now,
                last_used_at=now,
            )
            return client

    def reset_client(self, account: AccountContext) -> None:
        self._records.pop(self._build_cache_key(account), None)

    def reset_all(self) -> None:
        self._records.clear()

    def get_status(self, account: AccountContext) -> dict[str, object]:
        cache_key = self._build_cache_key(account)
        record = self._records.get(cache_key)
        return {
            "account_id": account.account_id,
            "wallet_address": account.wallet_address,
            "funder_address": account.funder_address,
            "proxy_address": account.proxy_address,
            "cached": record is not None,
            "created_at": record.created_at if record else None,
            "last_used_at": record.last_used_at if record else None,
        }


_clob_client_manager: ClobClientManager | None = None


def get_clob_client_manager() -> ClobClientManager:
    global _clob_client_manager
    if _clob_client_manager is None:
        _clob_client_manager = ClobClientManager()
    return _clob_client_manager
