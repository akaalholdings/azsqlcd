-- expect: UNSUPPORTED
-- says: MASKED WITH inside ALTER COLUMN with a data type
-- line: 4
ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] varchar(320) MASKED WITH (FUNCTION = 'email()') NOT NULL;
