-- expect: NF004
-- says: write decimal
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Amount] dec(18, 2) NOT NULL
);
