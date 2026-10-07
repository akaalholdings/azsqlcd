-- azsqlcd:migration 0001__drop_order
-- azsqlcd:mode tx
-- azsqlcd:allow DROP_TABLE [sales].[Order] reason: moved to the orders service
DROP TABLE [sales].[Order];
