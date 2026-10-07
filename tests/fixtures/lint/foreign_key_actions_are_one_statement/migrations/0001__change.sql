-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[OrderLine] WITH NOCHECK ADD CONSTRAINT [FK_OrderLine_Order]
    FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId])
    ON DELETE CASCADE ON UPDATE NO ACTION;
