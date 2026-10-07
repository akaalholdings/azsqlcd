-- expect: NF005
-- says: PRIMARY KEY
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    CONSTRAINT [PK_T] PRIMARY KEY ([Id])
);
