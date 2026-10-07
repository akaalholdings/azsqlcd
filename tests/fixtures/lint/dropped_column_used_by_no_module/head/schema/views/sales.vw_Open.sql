CREATE OR ALTER VIEW [sales].[vw_Open]
AS
SELECT [OrderId], [Status]
FROM [sales].[Order]
WHERE [Status] = 0 AND [Note] <> 'Stat';
