-- expect: SYNTAX
-- says: expected DEFAULT, PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK, found 'REFERENCES'
-- line: 4
ALTER TABLE [dbo].[T] ADD CONSTRAINT [FK_T_P] REFERENCES [dbo].[P] ([a]);
