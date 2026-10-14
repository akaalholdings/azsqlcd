CREATE TABLE [sales].[OrderLineStage] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] datetime2(0) NOT NULL,
    [Quantity] int NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
