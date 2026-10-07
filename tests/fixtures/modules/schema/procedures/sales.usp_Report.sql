CREATE OR ALTER PROCEDURE sales.usp_Report @Ids nvarchar(max) = N'' AS
SELECT sales.fn_Label(i.[Id], DEFAULT) AS [Label], l.[Qty], sales.fn_CustomerTotal(i.[Id]) AS [CustomerTotal]
FROM sales.fn_SplitIds(@Ids, N',') AS i
CROSS APPLY sales.fn_OrderLines(i.[Id]) AS l;
EXEC sales.usp_Export @From = '20200101';
