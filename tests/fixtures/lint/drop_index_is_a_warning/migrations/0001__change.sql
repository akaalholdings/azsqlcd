-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
DROP INDEX [IX_Order_Status] ON [sales].[Order];
