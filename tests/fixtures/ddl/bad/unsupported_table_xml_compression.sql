-- expect: UNSUPPORTED
-- says: table option XML_COMPRESSION
-- line: 8
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [a] xml NULL
)
WITH (XML_COMPRESSION = ON);
