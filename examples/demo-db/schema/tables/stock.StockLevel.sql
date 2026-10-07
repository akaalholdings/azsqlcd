CREATE TABLE [stock].[StockLevel] (
    [WarehouseId] smallint NOT NULL,
    [ProductId] int NOT NULL,
    [QuantityOnHand] int NOT NULL CONSTRAINT [DF_StockLevel_QuantityOnHand] DEFAULT (0),
    [QuantityReserved] int NOT NULL CONSTRAINT [DF_StockLevel_QuantityReserved] DEFAULT (0),
    [ReorderPoint] int NOT NULL CONSTRAINT [DF_StockLevel_ReorderPoint] DEFAULT (0),
    [QuantityAvailable] AS ([QuantityOnHand] - [QuantityReserved]),
    [UpdatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_StockLevel_UpdatedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_StockLevel] PRIMARY KEY CLUSTERED ([WarehouseId], [ProductId]),
    CONSTRAINT [CK_StockLevel_Quantities] CHECK ([QuantityOnHand] >= 0 AND [QuantityReserved] >= 0 AND [QuantityReserved] <= [QuantityOnHand]),
    CONSTRAINT [FK_StockLevel_Product] FOREIGN KEY ([ProductId]) REFERENCES [product].[Product] ([ProductId]),
    CONSTRAINT [FK_StockLevel_Warehouse] FOREIGN KEY ([WarehouseId]) REFERENCES [stock].[Warehouse] ([WarehouseId])
);
GO
CREATE NONCLUSTERED INDEX [IX_StockLevel_ProductId] ON [stock].[StockLevel] ([ProductId])
    INCLUDE ([QuantityOnHand], [QuantityReserved]);
