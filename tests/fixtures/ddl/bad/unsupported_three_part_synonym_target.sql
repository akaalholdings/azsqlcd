-- expect: UNSUPPORTED
-- says: three-part names
-- line: 5
-- path: schema/synonyms/dbo.Cust.sql
CREATE SYNONYM [dbo].[Cust] FOR [crm].[dbo].[Customer];
