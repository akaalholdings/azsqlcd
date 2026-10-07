-- azsqlcd:migration 0001__drop_stat
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_COLUMN [sales].[Order].[Stat] reason: replaced by Status in r30
ALTER TABLE [sales].[Order] DROP COLUMN [Stat];
