-- expect: SYNTAX
-- says: WITH VALUES
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NULL CONSTRAINT [DF_T_Id] DEFAULT (0) WITH VALUES
);
