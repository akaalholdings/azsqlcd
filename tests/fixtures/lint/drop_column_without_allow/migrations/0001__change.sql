-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] DROP COLUMN [LegacyCode];
