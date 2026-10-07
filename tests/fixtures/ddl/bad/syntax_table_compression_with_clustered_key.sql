-- expect: SYNTAX
-- says: is the compression of a heap
-- line: 5
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL,
    CONSTRAINT [PK_T] PRIMARY KEY CLUSTERED ([a])
)
WITH (DATA_COMPRESSION = PAGE);
