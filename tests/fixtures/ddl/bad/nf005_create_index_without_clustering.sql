-- expect: NF005
-- says: CREATE INDEX
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
);
GO
CREATE INDEX [IX_T_Id] ON [dbo].[T] ([Id]);
