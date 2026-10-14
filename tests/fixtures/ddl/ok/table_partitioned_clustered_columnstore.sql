-- path: schema/tables/dw.FactOrderLine.sql
CREATE TABLE [dw].[FactOrderLine] (
    [OrderDate] date NOT NULL,
    [ProductKey] int NOT NULL,
    [Amount] decimal(19, 4) NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactOrderLine] ON [dw].[FactOrderLine]
    ORDER ([ProductKey])
    WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE)
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_FactOrderLine_Product] ON [dw].[FactOrderLine] ([ProductKey])
    ON [PS_OrderYear]([OrderDate]);
