CREATE TABLE [sales].[Shipment] (
    [ShipmentId] bigint IDENTITY(1,1) NOT NULL CONSTRAINT [PK_Shipment] PRIMARY KEY CLUSTERED,
    [OrderId] int NOT NULL CONSTRAINT [FK_Shipment_Order] REFERENCES [sales].[Order] ([OrderId]),
    [ShippedUtc] datetime2(3) NULL,
    INDEX [IX_Shipment_Order] NONCLUSTERED ([OrderId])
);
