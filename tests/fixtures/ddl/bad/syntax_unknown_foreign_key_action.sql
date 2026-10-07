-- expect: SYNTAX
-- says: 'RESTRICT'
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [ParentId] int NULL,
    CONSTRAINT [FK_T_Parent] FOREIGN KEY ([ParentId]) REFERENCES [dbo].[Parent] ([Id])
        ON DELETE RESTRICT
);
