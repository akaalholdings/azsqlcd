CREATE OR ALTER VIEW [sales].[vw_OrderTotals]
WITH SCHEMABINDING
AS
-- Schema-bound: the engine refuses a change of the columns that this view names.
SELECT
    ol.[OrderId],
    COUNT_BIG(*) AS [LineCount],
    SUM(ol.[Quantity]) AS [UnitCount],
    SUM(ol.[LineTotal]) AS [OrderTotal]
FROM [sales].[OrderLine] AS ol
GROUP BY ol.[OrderId];
