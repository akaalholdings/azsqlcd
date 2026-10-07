CREATE OR ALTER VIEW [rpt].[vw_AliasTrap]
AS
SELECT fn_Tax.[OrderId], fn_Tax.[Total] AS [vw_OpenOrders]
FROM [sales].[vw_OrderTotals] AS fn_Tax;
