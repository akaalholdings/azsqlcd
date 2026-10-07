-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE [sales].[Order]
SET [Status] = CASE WHEN [ShippedUtc] IS NULL THEN 0 ELSE 1 END
WHERE [Status] IS NULL;
