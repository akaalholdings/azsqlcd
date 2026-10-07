CREATE OR ALTER VIEW [stock].[vw_StockAvailability]
AS
SELECT
    w.[WarehouseId],
    w.[Code] AS [WarehouseCode],
    p.[ProductId],
    p.[Sku],
    p.[Name] AS [ProductName],
    s.[QuantityOnHand],
    s.[QuantityReserved],
    s.[QuantityAvailable],
    s.[ReorderPoint]
FROM [stock].[StockLevel] AS s
JOIN [stock].[Warehouse] AS w ON w.[WarehouseId] = s.[WarehouseId]
JOIN [product].[Product] AS p ON p.[ProductId] = s.[ProductId];
