-- expect: SYNTAX
-- says: is the compression of a heap
-- line: 10
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL
)
WITH (DATA_COMPRESSION = PAGE);
GO
CREATE CLUSTERED INDEX [CX_T] ON [dbo].[T] ([a]);
