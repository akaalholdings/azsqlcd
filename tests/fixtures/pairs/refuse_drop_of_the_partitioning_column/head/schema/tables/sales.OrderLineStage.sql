CREATE TABLE [sales].[OrderLineStage] (
    [OrderLineId] bigint NOT NULL,
    [Quantity] int NOT NULL,
    [ShipDate] date NOT NULL
)
ON [PS_OrderYear]([ShipDate]);
