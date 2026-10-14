CREATE TABLE [sales].[OrderLineStage] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Quantity] int NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLineStage] ON [sales].[OrderLineStage] ([Quantity])
    ON [PS_OrderYear]([OrderDate]);
