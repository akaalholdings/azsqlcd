ALTER TABLE [sales].[OrderLine] WITH CHECK ADD CONSTRAINT [FK_OrderLine_Product] FOREIGN KEY ([TenantId], [ProductId]) REFERENCES [catalog].[Product] ([TenantId], [ProductId]);
