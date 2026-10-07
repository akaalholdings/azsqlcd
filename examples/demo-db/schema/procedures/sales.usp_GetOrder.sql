CREATE OR ALTER PROCEDURE [sales].[usp_GetOrder]
    @OrderId bigint
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        o.[OrderId],
        o.[OrderNumber],
        o.[CustomerId],
        c.[DisplayName] AS [CustomerName],
        o.[Status],
        [sales].[fn_OrderStatusName](o.[Status]) AS [StatusName],
        o.[CurrencyCode],
        o.[OrderedUtc],
        o.[ShippedUtc],
        ISNULL(t.[OrderTotal], 0) AS [OrderTotal]
    FROM [sales].[Order] AS o
    JOIN [sales].[Customer] AS c ON c.[CustomerId] = o.[CustomerId]
    LEFT JOIN [sales].[vw_OrderTotals] AS t ON t.[OrderId] = o.[OrderId]
    WHERE o.[OrderId] = @OrderId;

    SELECT
        l.[LineNumber],
        p.[Sku],
        p.[Name] AS [ProductName],
        l.[Quantity],
        l.[UnitPrice],
        l.[DiscountPercent],
        l.[LineTotal]
    FROM [sales].[OrderLine] AS l
    JOIN [product].[Product] AS p ON p.[ProductId] = l.[ProductId]
    WHERE l.[OrderId] = @OrderId
    ORDER BY l.[LineNumber];
END;
