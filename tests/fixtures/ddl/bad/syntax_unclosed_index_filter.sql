-- expect: SYNTAX
-- says: ')' to close the filter
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_T_a] ON [dbo].[T] ([a]) WHERE ([a] IS NOT NULL;
