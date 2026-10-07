-- expect: SYNTAX
-- says: an identifier cannot be empty
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [] int NOT NULL
);
