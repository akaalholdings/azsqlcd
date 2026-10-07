-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow LONG_LOCK [sales].[Order] reason: 2000 rows, read in under a second
ALTER TABLE [sales].[Order] ADD CONSTRAINT [CK_Order_Total] CHECK ([Total] >= 0);
