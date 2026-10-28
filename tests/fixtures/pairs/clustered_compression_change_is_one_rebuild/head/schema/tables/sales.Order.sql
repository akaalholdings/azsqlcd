CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [CustomerId] int NOT NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]) WITH (DATA_COMPRESSION = ROW, FILLFACTOR = 90),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
