-- expect: SYNTAX
-- says: one action per statement
-- line: 4
ALTER TABLE [dbo].[Customer] ALTER COLUMN [Email] DROP MASKED, COLUMN [Phone];
