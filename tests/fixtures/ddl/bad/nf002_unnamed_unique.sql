-- expect: NF002
-- says: UNIQUE
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL UNIQUE NONCLUSTERED
);
