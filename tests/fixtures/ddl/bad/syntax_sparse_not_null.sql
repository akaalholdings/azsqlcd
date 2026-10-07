-- expect: SYNTAX
-- says: a SPARSE column is NULL
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int SPARSE NOT NULL
);
