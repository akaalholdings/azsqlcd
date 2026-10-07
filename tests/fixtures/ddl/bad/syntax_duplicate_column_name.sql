-- expect: SYNTAX
-- says: two columns named
-- line: 5
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    [ID] int NULL
);
