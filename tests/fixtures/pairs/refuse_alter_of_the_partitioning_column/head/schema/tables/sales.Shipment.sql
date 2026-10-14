CREATE TABLE [sales].[Shipment] (
    [ShipmentId] int NOT NULL,
    [ShippedOn] datetime2(0) NOT NULL,
    CONSTRAINT [PK_Shipment] PRIMARY KEY CLUSTERED ([ShipmentId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Shipment_Id] ON [sales].[Shipment] ([ShipmentId])
    ON [PS_ShipYear]([ShippedOn]);
