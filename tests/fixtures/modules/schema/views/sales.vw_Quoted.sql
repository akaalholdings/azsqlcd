CREATE OR ALTER VIEW "sales"."vw_Quoted" AS
SELECT q."OrderId", x.[CustomerId]
FROM [sales]."vw_OpenOrders" AS q
JOIN SALES.VW_ACTIVECUSTOMERS AS x ON x.CustomerId = q."CustomerId";
