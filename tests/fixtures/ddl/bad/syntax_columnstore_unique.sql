-- expect: SYNTAX
-- says: a columnstore index cannot be UNIQUE
-- line: 4
CREATE UNIQUE CLUSTERED COLUMNSTORE INDEX [CCI_T] ON [dbo].[T];
