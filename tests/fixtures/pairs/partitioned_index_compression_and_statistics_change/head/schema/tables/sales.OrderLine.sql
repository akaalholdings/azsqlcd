CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Note] varchar(50) NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY NONCLUSTERED ([OrderLineId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate], [OrderLineId])
    WITH (DATA_COMPRESSION = PAGE)
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId])
    WITH (DATA_COMPRESSION = ROW, STATISTICS_INCREMENTAL = ON)
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity])
    ON [PRIMARY];
