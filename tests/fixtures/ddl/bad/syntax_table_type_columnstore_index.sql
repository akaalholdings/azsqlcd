-- expect: SYNTAX
-- says: cannot have a columnstore index
-- line: 5
-- path: schema/types/dbo.L.sql
CREATE TYPE [dbo].[L] AS TABLE (
    [a] int NOT NULL,
    INDEX [NCCI_L] NONCLUSTERED COLUMNSTORE ([a])
);
