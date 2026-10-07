DROP INDEX [IX_Order_Stat] ON [sales].[Order];
GO
ALTER TABLE [sales].[Order] DROP COLUMN [Stat];
GO
ALTER TABLE [sales].[Order] ADD [Status] tinyint NOT NULL;
GO
CREATE NONCLUSTERED INDEX [IX_Order_Stat] ON [sales].[Order] ([Status]) INCLUDE ([CustomerId]);
GO
