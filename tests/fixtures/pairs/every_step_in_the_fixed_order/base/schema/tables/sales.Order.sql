CREATE TABLE [sales].[Order] (
    [OrderId] int NOT NULL,
    [CustomerId] int NOT NULL,
    [Note] nvarchar(200) NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
