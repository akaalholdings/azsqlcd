CREATE OR ALTER VIEW [product].[vw_ActiveProducts]
AS
SELECT
    p.[ProductId],
    p.[Sku],
    p.[Name],
    c.[Name] AS [CategoryName],
    p.[ListPrice]
FROM [product].[Product] AS p
JOIN [product].[Category] AS c ON c.[CategoryId] = p.[CategoryId]
WHERE p.[IsActive] = 1 AND c.[IsActive] = 1;
