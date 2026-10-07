-- expect: SYNTAX
-- says: 'select'
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL,
    select int NULL
);
