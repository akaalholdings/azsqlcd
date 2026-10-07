-- expect: SYNTAX
-- says: the masking function as a string literal
-- line: 4
ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] ADD MASKED WITH (FUNCTION = email());
