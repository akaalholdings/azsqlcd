-- expect: SYNTAX
-- says: two indexes named [ix_t_id]
-- line: 11
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_T_Id] ON [dbo].[T] ([Id]);
GO
CREATE NONCLUSTERED INDEX [ix_t_id] ON [dbo].[T] ([Id] DESC);
