-- expect: SYNTAX
-- says: data type int takes no arguments
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int(4) NOT NULL
);
