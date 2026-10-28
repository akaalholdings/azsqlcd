CREATE TABLE [sales].[FactShipment] (
    [ShippedOn] date NOT NULL,
    [Amount] decimal(19, 4) NOT NULL
)
ON [PS_ShipYear]([ShippedOn]);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactShipment] ON [sales].[FactShipment]
    WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE)
    ON [PS_ShipYear]([ShippedOn]);
