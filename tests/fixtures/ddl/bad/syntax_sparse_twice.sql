-- expect: SYNTAX
-- says: SPARSE is written twice
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int SPARSE NULL SPARSE
);
