-- expect: NF001
-- says: [PhoneNumber]
-- line: 5
-- path: schema/types/dbo.PhoneNumber.sql
CREATE TYPE [dbo].[PhoneNumber] FROM varchar(20);
