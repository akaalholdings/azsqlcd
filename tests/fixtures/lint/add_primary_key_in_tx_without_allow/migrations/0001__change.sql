-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]);
