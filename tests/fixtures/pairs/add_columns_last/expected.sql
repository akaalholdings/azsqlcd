ALTER TABLE [sales].[Order] ADD [ShippedUtc] datetime2(3) NULL;
GO
ALTER TABLE [sales].[Order] ADD [Priority] tinyint NOT NULL CONSTRAINT [DF_Order_Priority] DEFAULT ((0));
GO
