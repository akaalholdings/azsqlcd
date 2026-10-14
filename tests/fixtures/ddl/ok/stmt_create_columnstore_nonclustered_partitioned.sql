CREATE NONCLUSTERED COLUMNSTORE INDEX [NCCI_OrderLine] ON [sales].[OrderLine] ([ProductId], [Quantity]) WHERE [Quantity] > 0 ON [PS_OrderYear]([OrderDate]);
