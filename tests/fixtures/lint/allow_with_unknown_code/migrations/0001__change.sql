-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_TABEL [sales].[Old] reason: replaced by sales.Order in r12
DROP TABLE [sales].[Old];
