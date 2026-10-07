CREATE OR ALTER FUNCTION [sales].[fn_CustomerOrders] (@CustomerId int)
RETURNS TABLE
AS
RETURN
(
    -- Inline table-valued function. It uses a view, so the view is deployed first.
    SELECT
        o.[OrderId],
        o.[OrderNumber],
        o.[Status],
        o.[OrderedUtc],
        o.[ShippedUtc],
        ISNULL(t.[OrderTotal], 0) AS [OrderTotal]
    FROM [sales].[Order] AS o
    LEFT JOIN [sales].[vw_OrderTotals] AS t ON t.[OrderId] = o.[OrderId]
    WHERE o.[CustomerId] = @CustomerId
);
