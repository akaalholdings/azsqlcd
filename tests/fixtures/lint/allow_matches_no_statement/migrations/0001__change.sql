-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow DATA_NO_WHERE [sales].[Order] reason: all rows get the new status
UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
