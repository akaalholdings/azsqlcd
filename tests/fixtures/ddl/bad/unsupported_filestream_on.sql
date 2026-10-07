-- expect: UNSUPPORTED
-- says: FILESTREAM
-- line: 7
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] int NOT NULL
) FILESTREAM_ON [fs];
