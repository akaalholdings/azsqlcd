-- path: schema/tables/dw.FactSale.sql
CREATE TABLE [dw].[FactSale] (
    [DateKey] int NOT NULL,
    [StoreKey] int NOT NULL,
    [Amount] decimal(19, 4) NOT NULL,
    CONSTRAINT [PK_FactSale] PRIMARY KEY NONCLUSTERED ([DateKey], [StoreKey])
);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactSale] ON [dw].[FactSale]
    ORDER ([DateKey], [StoreKey])
    WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE);
