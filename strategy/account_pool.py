from __future__ import annotations

from dataclasses import dataclass

from accounts import AccountContext


@dataclass
class AccountPool:
    accounts: list[AccountContext]

    def all_accounts(self) -> list[AccountContext]:
        return list(self.accounts)

    def get(self, account_id: str) -> AccountContext:
        for account in self.accounts:
            if account.account_id == account_id:
                return account
        raise KeyError(f"unknown account_id: {account_id}")

    def accounts_with_tag(self, tag: str) -> list[AccountContext]:
        return [account for account in self.accounts if tag in account.tags]

    def require_accounts(self, count: int, *, tag: str | None = None) -> list[AccountContext]:
        pool = self.accounts_with_tag(tag) if tag else self.all_accounts()
        if len(pool) < count:
            raise ValueError(f"requested {count} accounts but only {len(pool)} available")
        return pool[:count]
