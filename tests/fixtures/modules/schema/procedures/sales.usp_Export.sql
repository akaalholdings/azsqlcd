CREATE OR ALTER PROCEDURE [sales].[usp_Export]
    @From date,
    @To date = NULL
WITH EXECUTE AS OWNER
AS
SELECT o.[OrderId], t.[Total]
FROM [sales].[vw_OpenOrders] AS o
JOIN [sales].[vw_OrderTotals] AS t ON t.[OrderId] = o.[OrderId]
WHERE o.[OrderId] > 0 AND @From <= ISNULL(@To, @From);
