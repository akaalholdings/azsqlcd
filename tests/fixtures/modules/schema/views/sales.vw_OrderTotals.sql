CREATE OR ALTER VIEW [sales].[vw_OrderTotals] ([OrderId], [LineCount], [Total])
WITH SCHEMABINDING, VIEW_METADATA
AS
SELECT l.[OrderId], COUNT_BIG(*), SUM(l.[Qty] * l.[UnitPrice])
FROM [sales].[OrderLine] AS l
GROUP BY l.[OrderId];
