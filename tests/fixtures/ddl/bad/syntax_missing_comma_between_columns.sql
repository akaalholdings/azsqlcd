-- expect: SYNTAX
-- says: '[Name]'
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
    [Name] varchar(10) NULL
);
