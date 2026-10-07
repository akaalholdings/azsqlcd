-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_TABLE [sales].[Old] reason: TODO
DROP TABLE [sales].[Old];
