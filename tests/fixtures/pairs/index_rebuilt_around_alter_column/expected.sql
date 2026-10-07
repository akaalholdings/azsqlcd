DROP INDEX [IX_Order_Stat] ON [sales].[Order];
GO
ALTER TABLE [sales].[Order] ALTER COLUMN [Stat] smallint NOT NULL;
GO
CREATE NONCLUSTERED INDEX [IX_Order_Stat] ON [sales].[Order] ([Stat], [OrderId]);
GO
