-- expect: UNSUPPORTED
-- says: ledger tables
-- line: 9
-- path: schema/tables/audit.KeyCardEvent.sql
CREATE TABLE [audit].[KeyCardEvent] (
    [EmployeeId] int NOT NULL,
    [EventUtc] datetime2(7) NOT NULL
)
WITH (LEDGER = ON (APPEND_ONLY = ON));
