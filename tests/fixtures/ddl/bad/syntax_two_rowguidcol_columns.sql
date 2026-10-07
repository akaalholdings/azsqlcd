-- expect: SYNTAX
-- says: more than one ROWGUIDCOL column
-- line: 5
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] uniqueidentifier ROWGUIDCOL NOT NULL,
    [b] uniqueidentifier ROWGUIDCOL NOT NULL
);
