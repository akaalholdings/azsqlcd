ALTER TABLE [sales].[Order] DROP CONSTRAINT [FK_Order_Customer];
GO
DROP INDEX [IX_Order_Customer] ON [sales].[Order];
GO
DROP INDEX [IX_Order_Stat] ON [sales].[Order];
GO
ALTER TABLE [sales].[Order] DROP COLUMN [CustomerId];
GO
DROP TABLE [sales].[Customer];
GO
