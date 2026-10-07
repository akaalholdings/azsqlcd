-- expect: SYNTAX
-- says: 'REMOTE_DATA_ARCHIVE'
-- line: 8
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Id] int NOT NULL
)
WITH (REMOTE_DATA_ARCHIVE = ON);
