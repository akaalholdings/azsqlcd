CREATE TABLE [sales].[Shipment] (
    [ShipmentId] int NOT NULL,
    [ShippedOn] date NOT NULL,
    CONSTRAINT [PK_Shipment] PRIMARY KEY CLUSTERED ([ShippedOn], [ShipmentId]) WITH (STATISTICS_INCREMENTAL = ON) ON [PS_ShipYear]([ShippedOn])
)
ON [PS_ShipYear]([ShippedOn]);
