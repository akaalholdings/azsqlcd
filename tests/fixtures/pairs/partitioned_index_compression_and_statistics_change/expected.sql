DROP INDEX [CIX_OrderLine] ON [sales].[OrderLine];
GO
DROP INDEX [IX_OrderLine_Product] ON [sales].[OrderLine];
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate], [OrderLineId]) WITH (DATA_COMPRESSION = PAGE) ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId]) WITH (DATA_COMPRESSION = ROW, STATISTICS_INCREMENTAL = ON) ON [PS_OrderYear]([OrderDate]);
GO
