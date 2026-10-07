CREATE OR ALTER FUNCTION [sales].[fn_CustomerTotal] (@CustomerId int)
RETURNS decimal(19, 4)
WITH RETURNS NULL ON NULL INPUT, SCHEMABINDING, EXECUTE AS OWNER
AS
BEGIN
    RETURN (SELECT SUM(t.[Total])
            FROM [sales].[vw_OrderTotals] AS t
            JOIN [sales].[Order] AS o ON o.[OrderId] = t.[OrderId]
            WHERE o.[CustomerId] = @CustomerId);
END;
