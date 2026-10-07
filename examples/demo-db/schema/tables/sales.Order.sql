CREATE TABLE [sales].[Order] (
    [OrderId] bigint IDENTITY(1, 1) NOT NULL,
    [OrderNumber] int NOT NULL CONSTRAINT [DF_Order_OrderNumber] DEFAULT (NEXT VALUE FOR [sales].[OrderNumberSeq]),
    [CustomerId] int NOT NULL,
    [ShipToAddressId] int NULL,
    [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0),
    [CurrencyCode] char(3) NOT NULL,
    [OrderedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Order_OrderedUtc] DEFAULT (SYSUTCDATETIME()),
    [ShippedUtc] datetime2(3) NULL,
    [Note] nvarchar(1000) NULL,
    CONSTRAINT [PK_Order] PRIMARY KEY CLUSTERED ([OrderId]),
    CONSTRAINT [UQ_Order_OrderNumber] UNIQUE NONCLUSTERED ([OrderNumber]),
    CONSTRAINT [CK_Order_Shipped] CHECK ([ShippedUtc] IS NULL OR [ShippedUtc] >= [OrderedUtc]),
    CONSTRAINT [CK_Order_Status] CHECK ([Status] IN (0, 1, 2, 3, 4)),
    CONSTRAINT [FK_Order_Address] FOREIGN KEY ([ShipToAddressId]) REFERENCES [sales].[Address] ([AddressId]),
    CONSTRAINT [FK_Order_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Order_CustomerId] ON [sales].[Order] ([CustomerId], [OrderedUtc] DESC);
GO
CREATE NONCLUSTERED INDEX [IX_Order_Open] ON [sales].[Order] ([OrderedUtc])
    INCLUDE ([CustomerId])
    WHERE [Status] IN (0, 1);
