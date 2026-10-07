-- expect: NF004
-- says: write timestamp
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [RowVer] rowversion NOT NULL
);
