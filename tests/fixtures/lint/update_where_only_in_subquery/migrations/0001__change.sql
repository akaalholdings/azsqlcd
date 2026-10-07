-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE [sales].[Order]
SET [Total] = (SELECT SUM(l.[Amount]) FROM [sales].[OrderLine] AS l WHERE l.[Qty] > 0);
