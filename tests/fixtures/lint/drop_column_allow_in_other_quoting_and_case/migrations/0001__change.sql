-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_COLUMN sales.[order].LEGACYCODE reason: no reader since r30
ALTER TABLE [sales].[Order] DROP COLUMN [LegacyCode];
