-- expect: SYNTAX
-- says: ')' to close the expression
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] datetime2(7) NOT NULL CONSTRAINT [DF_T_a] DEFAULT (sysutcdatetime(
