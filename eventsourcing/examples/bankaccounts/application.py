from decimal import Decimal
from uuid import UUID

from eventsourcing.application import Application
from eventsourcing.examples.bankaccounts.domainmodel import (
    BankAccount,
)
from eventsourcing.repository import AggregateNotFoundError


class AccountNotFoundError(Exception):
    pass


class BankAccounts(Application):
    def open_account(
        self, full_name, email_address
    ) -> UUID:
        account = BankAccount.open(
            full_name=full_name,
            email_address=email_address,
        )
        self.save(account)
        return account.id

    def get_account(self, account_id: UUID) -> BankAccount:
        try:
            aggregate = self.repository.get(account_id)
        except AggregateNotFoundError:
            raise AccountNotFoundError(account_id)
        else:
            return aggregate

    def get_balance(self, account_id: UUID) -> Decimal:
        account = self.get_account(account_id)
        return account.balance

    def deposit_funds(
        self, credit_account_id: UUID, amount: Decimal
    ) -> None:
        account = self.get_account(credit_account_id)
        account.append_transaction(amount)
        self.save(account)

    def withdraw_funds(
        self, debit_account_id: UUID, amount: Decimal
    ) -> None:
        account = self.get_account(debit_account_id)
        account.append_transaction(-amount)
        self.save(account)

    def transfer_funds(
        self,
        debit_account_id: UUID,
        credit_account_id: UUID,
        amount: Decimal,
    ) -> None:
        debit_account = self.get_account(debit_account_id)
        credit_account = self.get_account(
            credit_account_id
        )
        debit_account.append_transaction(-amount)
        credit_account.append_transaction(amount)
        self.save(debit_account, credit_account)

    def set_overdraft_limit(
        self, account_id: UUID, overdraft_limit: Decimal
    ) -> None:
        account = self.get_account(account_id)
        account.set_overdraft_limit(overdraft_limit)
        self.save(account)

    def get_overdraft_limit(
        self, account_id: UUID
    ) -> Decimal:
        account = self.get_account(account_id)
        return account.overdraft_limit

    def close_account(self, account_id: UUID) -> None:
        account = self.get_account(account_id)
        account.close()
        self.save(account)