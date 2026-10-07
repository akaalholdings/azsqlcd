-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [dbo].[Customer] DROP CONSTRAINT IF EXISTS Throw
DROP TABLE [dbo].[Invoice];
