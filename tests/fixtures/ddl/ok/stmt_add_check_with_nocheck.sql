ALTER TABLE [sales].[OrderLine] WITH NOCHECK ADD CONSTRAINT [CK_OrderLine_Qty2] CHECK ([Quantity] < 500000);
