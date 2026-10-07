-- expect: SYNTAX
-- says: a known option
-- line: 4
CREATE NONCLUSTERED INDEX [IX_T] ON [dbo].[T] ([a]) WITH (COMPRESSION_DELAY = 5);
