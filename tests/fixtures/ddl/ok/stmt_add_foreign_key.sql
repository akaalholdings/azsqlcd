ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [FK_OrderLine_OrderHeader] FOREIGN KEY ([OrderId]) REFERENCES [sales].[OrderHeader] ([OrderId]) ON DELETE CASCADE ON UPDATE SET NULL;
