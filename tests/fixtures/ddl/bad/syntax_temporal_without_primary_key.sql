-- expect: SYNTAX
-- says: a system-versioned table needs a PRIMARY KEY
-- line: 5
-- path: schema/tables/dbo.Team.sql
CREATE TABLE [dbo].[Team] (
    [TeamId] int NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Team_History]));
