CREATE TABLE [sales].[FactOrderLine] (
    [OrderDate] date NOT NULL,
    [ProductKey] int NOT NULL,
    [Amount] decimal(19, 4) NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactOrderLine] ON [sales].[FactOrderLine]
    WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE)
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_FactOrderLine_Product] ON [sales].[FactOrderLine] ([ProductKey])
    ON [PS_OrderYear]([OrderDate]);
