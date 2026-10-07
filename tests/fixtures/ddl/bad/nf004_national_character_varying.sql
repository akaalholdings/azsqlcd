-- expect: NF004
-- says: write nvarchar
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Name] national character varying(50) NULL
);
