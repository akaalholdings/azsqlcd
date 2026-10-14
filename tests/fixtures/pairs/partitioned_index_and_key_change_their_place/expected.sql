DROP INDEX [IX_OrderLine_Product] ON [sales].[OrderLine];
GO
DROP INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine];
GO
ALTER TABLE [sales].[OrderLine] DROP CONSTRAINT [UQ_OrderLine_Date_Line];
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [UQ_OrderLine_Date_Line] UNIQUE NONCLUSTERED ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId]) ON [PRIMARY];
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity]) ON [PS_Quantity]([Quantity]);
GO
