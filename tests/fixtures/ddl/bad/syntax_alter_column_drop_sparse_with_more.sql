-- expect: SYNTAX
-- says: the end of the statement
-- line: 4
ALTER TABLE [dbo].[T] ALTER COLUMN [c] DROP SPARSE WITH (ONLINE = ON);
