-- path: schema/tables/audit.Login.sql
CREATE TABLE [audit].[Login] (
    [LoginId] bigint NOT NULL,
    [Account] nvarchar(128) NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    CONSTRAINT [PK_Login] PRIMARY KEY NONCLUSTERED ([LoginId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [archive].[Login], HISTORY_RETENTION_PERIOD = 6 MONTHS));
