-- expect: SYNTAX
-- says: the masking function cannot be empty
-- line: 4
ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] ADD MASKED WITH (FUNCTION = '  ');
