-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DELETE FROM [sales].[Staging]
DELETE FROM [sales].[Order] WHERE [OrderId] = 7
