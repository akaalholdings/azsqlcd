-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] WITH NOCHECK ADD CONSTRAINT [FK_Order_Customer]
    FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]);
