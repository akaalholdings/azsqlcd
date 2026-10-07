-- expect: SYNTAX
-- says: expected DEFAULT, PRIMARY KEY, UNIQUE, FOREIGN KEY, REFERENCES or CHECK, found 'NOT'
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int CONSTRAINT [NN_T_a] NOT NULL
);
