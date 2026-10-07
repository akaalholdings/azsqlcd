-- expect: SYNTAX
-- says: 'CREATE'
-- line: 8
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
);
CREATE NONCLUSTERED INDEX [IX_T_Id] ON [dbo].[T] ([Id]);
