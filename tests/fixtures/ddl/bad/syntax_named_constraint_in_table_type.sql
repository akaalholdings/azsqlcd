-- expect: SYNTAX
-- says: cannot be named
-- line: 7
-- path: schema/types/dbo.IdList.sql
CREATE TYPE [dbo].[IdList] AS TABLE (
    [Id] int NOT NULL,
    CONSTRAINT [PK_IdList] PRIMARY KEY CLUSTERED ([Id])
);
