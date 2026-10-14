ALTER TABLE [sales].[OrderLine] DROP CONSTRAINT [PK_OrderLine];
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [PK_OrderLine] PRIMARY KEY NONCLUSTERED ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate]) ON [PS_OrderYear]([OrderDate]);
GO
