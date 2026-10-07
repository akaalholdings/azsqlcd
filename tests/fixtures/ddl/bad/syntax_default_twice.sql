-- expect: SYNTAX
-- says: DEFAULT is written twice
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL CONSTRAINT [DF_T_a] DEFAULT ((0)) CONSTRAINT [DF_T_a2] DEFAULT ((1))
);
