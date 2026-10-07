-- expect: SYNTAX
-- says: written as an element of the table
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL INDEX [NCCI_T] NONCLUSTERED COLUMNSTORE
);
