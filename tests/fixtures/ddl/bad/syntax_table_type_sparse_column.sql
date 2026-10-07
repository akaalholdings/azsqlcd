-- expect: SYNTAX
-- says: cannot have a SPARSE column
-- line: 5
-- path: schema/types/dbo.L.sql
CREATE TYPE [dbo].[L] AS TABLE (
    [a] int SPARSE NULL
);
