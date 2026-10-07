CREATE TABLE [stock].[StockMovement] (
    [MovementId] bigint NOT NULL CONSTRAINT [DF_StockMovement_MovementId] DEFAULT (NEXT VALUE FOR [stock].[MovementIdSeq]),
    [WarehouseId] smallint NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Reason] varchar(20) NOT NULL,
    [OrderId] bigint NULL,
    [MovedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_StockMovement_MovedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_StockMovement] PRIMARY KEY CLUSTERED ([MovementId]),
    CONSTRAINT [CK_StockMovement_Quantity] CHECK ([Quantity] <> 0),
    CONSTRAINT [CK_StockMovement_Reason] CHECK ([Reason] IN ('receipt', 'shipment', 'adjustment', 'return')),
    CONSTRAINT [FK_StockMovement_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]),
    CONSTRAINT [FK_StockMovement_StockLevel] FOREIGN KEY ([WarehouseId], [ProductId]) REFERENCES [stock].[StockLevel] ([WarehouseId], [ProductId])
);
GO
CREATE NONCLUSTERED INDEX [IX_StockMovement_Product] ON [stock].[StockMovement] ([ProductId], [MovedUtc] DESC)
    WITH (DATA_COMPRESSION = PAGE);
