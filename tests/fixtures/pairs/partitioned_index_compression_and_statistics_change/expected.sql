DROP INDEX [IX_OrderLine_Product] ON [sales].[OrderLine];
GO
ALTER TABLE [sales].[OrderLine] REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = PAGE);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId]) WITH (DATA_COMPRESSION = ROW, STATISTICS_INCREMENTAL = ON) ON [PS_OrderYear]([OrderDate]);
GO
