-- azsqlcd:migration 0001__backfill
-- azsqlcd:mode tx
-- azsqlcd:data
DECLARE @doc xml = N'<r/>';
UPDATE o SET [Note] = N'dbo.fn_Round' /* fn_Round */
FROM [sales].[Order] AS o
WHERE o.fn_Round = 1 AND sales.fn_Round(1) = 1 AND @doc.fn_Round('/r') = 1;
