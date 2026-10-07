-- expect: NF002
-- says: CHECK
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    CHECK ([Id] > 0)
);
