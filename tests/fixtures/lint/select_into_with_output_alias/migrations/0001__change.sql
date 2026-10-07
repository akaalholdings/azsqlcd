-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
SELECT [Status] AS OUTPUT INTO [sales].[OrderCopy] FROM [sales].[Order];
