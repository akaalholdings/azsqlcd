-- expect: NF002
-- says: FOREIGN KEY
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [ParentId] int NULL REFERENCES [dbo].[Parent] ([Id])
);
