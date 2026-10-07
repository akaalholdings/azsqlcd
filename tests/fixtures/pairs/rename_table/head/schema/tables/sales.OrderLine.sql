CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] int NOT NULL,
    [OrderId] int NOT NULL,
    [CustomerId] int NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderLineId]),
    CONSTRAINT [FK_OrderLine_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId])
);
