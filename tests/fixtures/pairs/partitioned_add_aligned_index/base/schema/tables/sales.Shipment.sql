CREATE TABLE [sales].[Shipment] (
    [ShipmentId] int NOT NULL,
    [ShippedOn] date NOT NULL,
    CONSTRAINT [PK_Shipment] PRIMARY KEY CLUSTERED ([ShipmentId])
);
