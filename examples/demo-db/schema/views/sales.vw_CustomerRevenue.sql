CREATE OR ALTER VIEW [sales].[vw_CustomerRevenue]
AS
SELECT
    c.[CustomerId],
    c.[DisplayName],
    c.[CountryCode],
    COUNT(o.[OrderId]) AS [OrderCount],
    ISNULL(SUM(t.[OrderTotal]), 0) AS [Revenue],
    MAX(o.[OrderedUtc]) AS [LastOrderedUtc]
FROM [sales].[Customer] AS c
LEFT JOIN [sales].[Order] AS o ON o.[CustomerId] = c.[CustomerId] AND o.[Status] <> 4
LEFT JOIN [sales].[vw_OrderTotals] AS t ON t.[OrderId] = o.[OrderId]
GROUP BY c.[CustomerId], c.[DisplayName], c.[CountryCode];
