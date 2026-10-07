CREATE OR ALTER PROCEDURE [sales].[usp_ShipOrder]
    @OrderId bigint,
    @WarehouseId smallint
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @Status tinyint = (SELECT [Status] FROM [sales].[Order] WHERE [OrderId] = @OrderId);

    IF @Status IS NULL
        THROW 50020, N'The order does not exist.', 1;
    IF @Status <> 1
        THROW 50021, N'Only a paid order can be shipped.', 1;

    BEGIN TRANSACTION;

    -- The CHECK constraint [CK_StockLevel_Quantities] refuses a quantity below zero.
    UPDATE s
    SET s.[QuantityOnHand] = s.[QuantityOnHand] - l.[Quantity],
        s.[UpdatedUtc] = SYSUTCDATETIME()
    FROM [stock].[StockLevel] AS s
    JOIN [sales].[OrderLine] AS l ON l.[ProductId] = s.[ProductId]
    WHERE s.[WarehouseId] = @WarehouseId
      AND l.[OrderId] = @OrderId;

    IF @@ROWCOUNT <> (SELECT COUNT(*) FROM [sales].[OrderLine] WHERE [OrderId] = @OrderId)
        THROW 50022, N'The warehouse does not stock every product of the order.', 1;

    INSERT INTO [stock].[StockMovement] ([WarehouseId], [ProductId], [Quantity], [Reason], [OrderId])
    SELECT @WarehouseId, l.[ProductId], -l.[Quantity], 'shipment', l.[OrderId]
    FROM [sales].[OrderLine] AS l
    WHERE l.[OrderId] = @OrderId;

    UPDATE [sales].[Order]
    SET [Status] = 2,
        [ShippedUtc] = SYSUTCDATETIME()
    WHERE [OrderId] = @OrderId;

    COMMIT TRANSACTION;
END;
