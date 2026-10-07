-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
CREATE NONCLUSTERED INDEX [IX_Order_Status] ON [sales].[Order] ([Status]);
