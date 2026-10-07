-- expect: SYNTAX
-- says: 'BUCKET_SIZE'
-- line: 5
CREATE NONCLUSTERED INDEX [IX_T_Id] ON [dbo].[T] ([Id])
    WITH (FILLFACTOR = 90, BUCKET_SIZE = 4);
