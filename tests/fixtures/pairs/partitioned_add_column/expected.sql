ALTER TABLE [sales].[OrderLine] ADD [Discount] decimal(5, 2) NOT NULL CONSTRAINT [DF_OrderLine_Discount] DEFAULT ((0));
GO
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [CK_OrderLine_Quantity] CHECK ([Quantity] > 0);
GO
