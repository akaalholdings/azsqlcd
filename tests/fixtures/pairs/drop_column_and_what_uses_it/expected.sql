ALTER TABLE [sales].[Order] DROP CONSTRAINT [CK_Order_Legacy];
GO
ALTER TABLE [sales].[Order] DROP CONSTRAINT [DF_Order_Legacy];
GO
DROP INDEX [IX_Order_Legacy] ON [sales].[Order];
GO
ALTER TABLE [sales].[Order] DROP COLUMN [Legacy];
GO
