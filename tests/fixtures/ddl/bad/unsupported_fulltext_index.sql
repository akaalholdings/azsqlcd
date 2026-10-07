-- expect: UNSUPPORTED
-- says: full-text indexes
-- line: 4
CREATE FULLTEXT INDEX ON [dbo].[Doc] ([Body]) KEY INDEX [PK_Doc];
