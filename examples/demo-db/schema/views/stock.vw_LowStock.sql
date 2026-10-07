CREATE OR ALTER VIEW [stock].[vw_LowStock]
AS
SELECT
    a.[WarehouseCode],
    a.[Sku],
    a.[ProductName],
    a.[QuantityAvailable],
    a.[ReorderPoint]
FROM [stock].[vw_StockAvailability] AS a
WHERE a.[QuantityAvailable] <= a.[ReorderPoint];
