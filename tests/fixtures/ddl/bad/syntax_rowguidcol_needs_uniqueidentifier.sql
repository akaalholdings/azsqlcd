-- expect: SYNTAX
-- says: ROWGUIDCOL goes with the data type uniqueidentifier
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int ROWGUIDCOL NOT NULL
);
