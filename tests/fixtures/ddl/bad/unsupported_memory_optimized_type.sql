-- expect: UNSUPPORTED
-- says: memory-optimized table types
-- line: 7
-- path: schema/types/dbo.Rows.sql
CREATE TYPE [dbo].[Rows] AS TABLE (
    [a] int NOT NULL
) WITH (MEMORY_OPTIMIZED = ON);
