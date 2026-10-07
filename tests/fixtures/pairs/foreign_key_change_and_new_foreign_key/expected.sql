ALTER TABLE [sales].[OrderLine] DROP CONSTRAINT [FK_OrderLine_Order];
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [FK_OrderLine_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]);
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [FK_OrderLine_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]) ON DELETE CASCADE;
GO
