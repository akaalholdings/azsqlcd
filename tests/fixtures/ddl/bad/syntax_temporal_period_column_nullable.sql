-- expect: SYNTAX
-- says: period column [ValidTo] must be NOT NULL
-- line: 8
-- path: schema/tables/dbo.Team.sql
CREATE TABLE [dbo].[Team] (
    [TeamId] int NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NULL,
    CONSTRAINT [PK_Team] PRIMARY KEY CLUSTERED ([TeamId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON (HISTORY_TABLE = [dbo].[Team_History]));
