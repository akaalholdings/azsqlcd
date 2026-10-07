-- expect: NF006
-- says: needs the history table by name
-- line: 12
-- path: schema/tables/dbo.Team.sql
CREATE TABLE [dbo].[Team] (
    [TeamId] int NOT NULL,
    [ValidFrom] datetime2(7) GENERATED ALWAYS AS ROW START NOT NULL,
    [ValidTo] datetime2(7) GENERATED ALWAYS AS ROW END NOT NULL,
    CONSTRAINT [PK_Team] PRIMARY KEY CLUSTERED ([TeamId]),
    PERIOD FOR SYSTEM_TIME ([ValidFrom], [ValidTo])
)
WITH (SYSTEM_VERSIONING = ON);
