CREATE TABLE [sales].[OrderLineStage] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Quantity] int NOT NULL,
    [ShipDate] date NOT NULL
)
ON [PS_OrderYear]([ShipDate]);
