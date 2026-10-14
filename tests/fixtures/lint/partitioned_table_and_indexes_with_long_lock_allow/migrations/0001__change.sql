-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Quantity] int NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate])
)
ON [PS_OrderYear]([OrderDate])
WITH (DATA_COMPRESSION = PAGE);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity])
    WITH (STATISTICS_INCREMENTAL = ON)
    ON [PS_OrderYear]([OrderDate]);
GO
-- azsqlcd:allow LONG_LOCK [sales].[Shipment] reason: 2000 rows, built in under a second
CREATE NONCLUSTERED INDEX [IX_Shipment_Date] ON [sales].[Shipment] ([ShipDate])
    ON [PS_ShipYear]([ShipDate]);
GO
-- azsqlcd:allow LONG_LOCK [sales].[Shipment] reason: 2000 rows, built in under a second
ALTER TABLE [sales].[Shipment] ADD CONSTRAINT [UQ_Shipment] UNIQUE NONCLUSTERED ([ShipmentId])
    ON [PRIMARY];
GO
-- azsqlcd:allow LONG_LOCK [sales].[OrderLineStage] reason: 2000 rows, written in under a second
ALTER TABLE [sales].[OrderLineStage] REBUILD PARTITION = ALL WITH (DATA_COMPRESSION = PAGE);
