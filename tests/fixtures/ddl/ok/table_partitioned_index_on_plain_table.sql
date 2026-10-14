-- path: schema/tables/sales.Shipment.sql
CREATE TABLE [sales].[Shipment] (
    [ShipmentId] int NOT NULL,
    [ShippedOn] date NOT NULL,
    [Carrier] varchar(20) NULL,
    CONSTRAINT [PK_Shipment] PRIMARY KEY CLUSTERED ([ShipmentId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Shipment_Carrier] ON [sales].[Shipment] ([Carrier]);
GO
CREATE NONCLUSTERED INDEX [IX_Shipment_ShippedOn] ON [sales].[Shipment] ([ShippedOn])
    ON [PS_ShipYear]([ShippedOn]);
