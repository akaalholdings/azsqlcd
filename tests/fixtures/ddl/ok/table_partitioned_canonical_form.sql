-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY NONCLUSTERED ([OrderLineId]) ON [PRIMARY],
    CONSTRAINT [UQ_OrderLine_Date_Line] UNIQUE NONCLUSTERED ([OrderDate], [OrderLineId]) WITH (DATA_COMPRESSION = PAGE) ON [PS_OrderYear]([OrderDate])
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate], [OrderLineId])
    WITH (DATA_COMPRESSION = PAGE)
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId])
    INCLUDE ([Quantity])
    WITH (STATISTICS_INCREMENTAL = ON)
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity])
    WHERE [Quantity] > 100
    ON [PRIMARY];
