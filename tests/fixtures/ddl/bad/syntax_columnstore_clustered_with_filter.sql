-- expect: SYNTAX
-- says: has no filter
-- line: 4
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_T] ON [dbo].[T] WHERE [a] > 0;
