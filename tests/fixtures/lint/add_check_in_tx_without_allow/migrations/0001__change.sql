-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] WITH CHECK ADD CONSTRAINT [CK_Order_Total] CHECK ([Total] >= 0);
