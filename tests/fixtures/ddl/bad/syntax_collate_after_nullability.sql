-- expect: SYNTAX
-- says: COLLATE must directly follow
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Name] varchar(10) NOT NULL COLLATE Latin1_General_BIN2
);
