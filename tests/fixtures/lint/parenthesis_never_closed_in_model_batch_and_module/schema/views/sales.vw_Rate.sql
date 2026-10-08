CREATE OR ALTER VIEW [sales].[vw_Rate]
AS
SELECT ROUND(([Rate] * 100, 2) AS [Percent]
FROM [sales].[Region];
