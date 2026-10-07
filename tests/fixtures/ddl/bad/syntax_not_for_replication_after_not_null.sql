-- expect: SYNTAX
-- says: NOT FOR REPLICATION goes directly after IDENTITY
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int IDENTITY(1, 1) NOT NULL NOT FOR REPLICATION
);
