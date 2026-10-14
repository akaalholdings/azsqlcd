CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity]) WHERE [Quantity] > 100 ON [PRIMARY];
