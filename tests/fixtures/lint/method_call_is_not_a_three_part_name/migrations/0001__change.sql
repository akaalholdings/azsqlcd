-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DECLARE @doc xml = N'<r><id>7</id></r>';
UPDATE o SET o.[Status] = 1
FROM [sales].[Order] AS o
JOIN @doc.nodes('/r/id') AS n(c) ON o.[OrderId] = n.c.value('.', 'int')
WHERE o.[Status] IS NULL;
