-- expect: UNSUPPORTED
-- says: ledger tables (GENERATED ALWAYS)
-- line: 7
-- path: schema/tables/account.Balance.sql
CREATE TABLE [account].[Balance] (
    [CustomerId] int NOT NULL,
    [LedgerStartTx] bigint GENERATED ALWAYS AS TRANSACTION_ID START HIDDEN NOT NULL
);
