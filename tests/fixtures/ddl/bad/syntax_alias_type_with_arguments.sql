-- expect: SYNTAX
-- says: an alias type takes no arguments
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] [dbo].[Code](10) NOT NULL
);
