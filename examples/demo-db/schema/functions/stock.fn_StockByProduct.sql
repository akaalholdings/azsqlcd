CREATE OR ALTER FUNCTION [stock].[fn_StockByProduct] (@ProductId int)
RETURNS @Stock TABLE
(
    [WarehouseCode] char(4) NOT NULL,
    [QuantityOnHand] int NOT NULL,
    [QuantityReserved] int NOT NULL,
    [QuantityAvailable] int NOT NULL,
    [NeedsReorder] bit NOT NULL
)
AS
BEGIN
    -- Multi-statement table-valued function: one row for each warehouse that stocks the product,
    -- or one row with the code '----' when no warehouse stocks it.
    INSERT INTO @Stock ([WarehouseCode], [QuantityOnHand], [QuantityReserved], [QuantityAvailable], [NeedsReorder])
    SELECT
        w.[Code],
        s.[QuantityOnHand],
        s.[QuantityReserved],
        s.[QuantityAvailable],
        CASE WHEN s.[QuantityAvailable] <= s.[ReorderPoint] THEN 1 ELSE 0 END
    FROM [stock].[StockLevel] AS s
    JOIN [stock].[Warehouse] AS w ON w.[WarehouseId] = s.[WarehouseId]
    WHERE s.[ProductId] = @ProductId;

    IF NOT EXISTS (SELECT 1 FROM @Stock)
        INSERT INTO @Stock ([WarehouseCode], [QuantityOnHand], [QuantityReserved], [QuantityAvailable], [NeedsReorder])
        VALUES ('----', 0, 0, 0, 1);

    RETURN;
END;
