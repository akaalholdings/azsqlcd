-- expect: SYNTAX
-- says: unterminated string
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [Name] varchar(5) NULL CONSTRAINT [DF_T_Name] DEFAULT ('abc)
);
