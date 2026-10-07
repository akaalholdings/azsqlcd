-- expect: SYNTAX
-- says: NONE or ROW or PAGE for DATA_COMPRESSION
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL,
    INDEX [IX_T_a] NONCLUSTERED ([a]) WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE)
);
