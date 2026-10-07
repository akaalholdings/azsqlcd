-- expect: UNSUPPORTED
-- says: vector indexes
-- line: 4
CREATE VECTOR INDEX [VIX_Embedding_Vec] ON [dbo].[Embedding] ([Vec]) WITH (METRIC = 'cosine', TYPE = 'diskann');
