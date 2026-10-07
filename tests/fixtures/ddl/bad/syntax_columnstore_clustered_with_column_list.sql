-- expect: SYNTAX
-- says: has no column list
-- line: 4
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_T] ON [dbo].[T] ([a]);
