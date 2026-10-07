-- expect: SYNTAX
-- says: expected PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK, found 'REFERENCES'
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL,
    CONSTRAINT [FK_T_P] REFERENCES [dbo].[P] ([a])
);
