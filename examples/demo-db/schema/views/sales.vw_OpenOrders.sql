CREATE OR ALTER VIEW [sales].[vw_OpenOrders]
AS
SELECT
    o.[OrderId],
    o.[OrderNumber],
    o.[CustomerId],
    c.[DisplayName] AS [CustomerName],
    o.[Status],
    [sales].[fn_OrderStatusName](o.[Status]) AS [StatusName],
    o.[CurrencyCode],
    o.[OrderedUtc],
    ISNULL(t.[OrderTotal], 0) AS [OrderTotal]
FROM [sales].[Order] AS o
JOIN [sales].[Customer] AS c ON c.[CustomerId] = o.[CustomerId]
LEFT JOIN [sales].[vw_OrderTotals] AS t ON t.[OrderId] = o.[OrderId]
WHERE o.[Status] IN (0, 1);
