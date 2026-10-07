-- expect: SYNTAX
-- says: IDENTITY is written twice
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int IDENTITY(1, 1) IDENTITY(5, 5) NOT NULL
);
