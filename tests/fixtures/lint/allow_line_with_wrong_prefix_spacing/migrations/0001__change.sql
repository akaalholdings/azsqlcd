-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
--azsqlcd:allow DROP_COLUMN [sales].[Order].[LegacyCode] reason: replaced by Code in r41
ALTER TABLE [sales].[Order] DROP COLUMN [LegacyCode];
