-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
SELECT [OrderId], [Status]
INTO [sales].[OrderCopy]
FROM [sales].[Order];
