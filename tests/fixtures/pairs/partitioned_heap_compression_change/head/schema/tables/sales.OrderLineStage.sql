CREATE TABLE [sales].[OrderLineStage] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Quantity] int NOT NULL
)
ON [PS_OrderYear]([OrderDate])
WITH (DATA_COMPRESSION = PAGE);
