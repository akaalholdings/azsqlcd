-- expect: SYNTAX
-- says: ON DELETE is written twice
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NULL,
    CONSTRAINT [FK_T_P] FOREIGN KEY ([a]) REFERENCES [dbo].[P] ([a]) ON DELETE CASCADE ON DELETE SET NULL
);
