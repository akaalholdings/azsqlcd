CREATE OR ALTER VIEW [sales].[vw_Open]
AS
SELECT [OrderId], [Stat]
FROM [sales].[Order]
WHERE [Stat] = 0;
