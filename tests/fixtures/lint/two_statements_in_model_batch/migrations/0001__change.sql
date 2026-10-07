-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
DROP INDEX [IX_Old_Status] ON [sales].[Old];
DROP TABLE [sales].[Old];
