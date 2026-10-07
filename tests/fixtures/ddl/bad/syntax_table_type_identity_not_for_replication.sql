-- expect: SYNTAX
-- says: cannot have NOT FOR REPLICATION
-- line: 5
-- path: schema/types/dbo.L.sql
CREATE TYPE [dbo].[L] AS TABLE (
    [a] int IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL
);
