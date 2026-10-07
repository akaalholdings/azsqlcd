CREATE OR ALTER PROCEDURE [stock].[usp_AdjustStock]
    @WarehouseId smallint,
    @ProductId int,
    @Quantity int,
    @Reason varchar(20) = 'adjustment',
    @OrderId bigint = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    IF @Quantity = 0
        THROW 50030, N'The quantity of a stock movement is not zero.', 1;

    BEGIN TRANSACTION;

    UPDATE [stock].[StockLevel] WITH (UPDLOCK, SERIALIZABLE)
    SET [QuantityOnHand] = [QuantityOnHand] + @Quantity,
        [UpdatedUtc] = SYSUTCDATETIME()
    WHERE [WarehouseId] = @WarehouseId
      AND [ProductId] = @ProductId;

    IF @@ROWCOUNT = 0
        INSERT INTO [stock].[StockLevel] ([WarehouseId], [ProductId], [QuantityOnHand])
        VALUES (@WarehouseId, @ProductId, @Quantity);

    INSERT INTO [stock].[StockMovement] ([WarehouseId], [ProductId], [Quantity], [Reason], [OrderId])
    VALUES (@WarehouseId, @ProductId, @Quantity, @Reason, @OrderId);

    COMMIT TRANSACTION;
END;
