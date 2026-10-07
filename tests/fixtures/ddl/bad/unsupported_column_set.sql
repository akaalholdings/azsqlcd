-- expect: UNSUPPORTED
-- says: column sets
-- line: 7
-- path: schema/tables/dbo.Document.sql
CREATE TABLE [dbo].[Document] (
    [DocumentId] int NOT NULL,
    [Extra] xml COLUMN_SET FOR ALL_SPARSE_COLUMNS
);
