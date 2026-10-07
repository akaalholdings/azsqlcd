CREATE OR ALTER FUNCTION [sales].[fn_OrderLines] (@OrderId int)
RETURNS TABLE WITH SCHEMABINDING AS RETURN
(
    SELECT l.[OrderId], l.[LineNo], l.[Qty], l.[UnitPrice]
    FROM [sales].[OrderLine] AS l
    WHERE l.[OrderId] = @OrderId
);
