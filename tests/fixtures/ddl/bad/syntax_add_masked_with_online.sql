-- expect: SYNTAX
-- says: expected the end of the statement
-- line: 4
ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] ADD MASKED WITH (FUNCTION = 'email()') WITH (ONLINE = ON);
