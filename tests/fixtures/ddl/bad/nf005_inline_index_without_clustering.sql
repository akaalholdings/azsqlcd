-- expect: NF005
-- says: [IX_T_Id]
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    INDEX [IX_T_Id] ([Id])
);
