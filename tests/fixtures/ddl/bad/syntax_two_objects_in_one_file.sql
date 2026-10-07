-- expect: SYNTAX
-- says: one object per file
-- line: 9
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
);
GO
CREATE TABLE [dbo].[U] (
    [Id] int NOT NULL
);
