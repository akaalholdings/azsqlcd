-- expect: UNSUPPORTED
-- says: XML schema collections
-- line: 6
-- path: schema/tables/dbo.T.sql
CREATE TABLE [dbo].[T] (
    [Body] xml(CONTENT [dbo].[OrderSchema]) NULL
);
