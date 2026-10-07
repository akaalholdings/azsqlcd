ALTER TABLE [sales].[Order] DROP CONSTRAINT [FK_Order_Customer];
GO
ALTER TABLE [sales].[Customer] DROP CONSTRAINT [PK_Customer];
GO
ALTER TABLE [sales].[Customer] ADD CONSTRAINT [PK_Customer] PRIMARY KEY NONCLUSTERED ([CustomerId]) WITH (FILLFACTOR = 90);
GO
ALTER TABLE [sales].[Order] ADD CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]);
GO
