-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
CREATE TABLE [sales].[Refund] (
    [RefundId] int NOT NULL CONSTRAINT [PK_Refund] PRIMARY KEY CLUSTERED,
    [OrderId] int NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_Refund_OrderId] ON sales.refund ([OrderId]);
