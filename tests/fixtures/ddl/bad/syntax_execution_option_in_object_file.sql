-- expect: SYNTAX
-- says: execution option
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_T_Id] ON [dbo].[T] ([Id]) WITH (ONLINE = ON);
