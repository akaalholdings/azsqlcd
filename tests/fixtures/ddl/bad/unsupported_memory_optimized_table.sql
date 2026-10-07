-- expect: UNSUPPORTED
-- says: memory-optimized tables
-- line: 9
-- path: schema/tables/dbo.Hot.sql
CREATE TABLE [dbo].[Hot] (
    [Id] int NOT NULL,
    CONSTRAINT [PK_Hot] PRIMARY KEY NONCLUSTERED ([Id])
)
WITH (MEMORY_OPTIMIZED = ON, DURABILITY = SCHEMA_AND_DATA);
