-- expect: SYNTAX
-- says: is on [dbo].[U]
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_U_Id] ON [dbo].[U] ([Id]);
