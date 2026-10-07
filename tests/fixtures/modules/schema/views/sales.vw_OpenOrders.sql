/* Open orders.
   History: CREATE VIEW first written in 2019, AS a draft.
GO
*/
-- CREATE OR ALTER VIEW [sales].[vw_Wrong] AS SELECT 1
CREATE OR ALTER VIEW [sales].[vw_OpenOrders]
AS
SELECT o.[OrderId], o.[CustomerId], [sales].[fn_Tax](o.[Total], DEFAULT) AS [Tax]
FROM [sales].[Order] AS o
WHERE o.[ShippedUtc] IS NULL;
