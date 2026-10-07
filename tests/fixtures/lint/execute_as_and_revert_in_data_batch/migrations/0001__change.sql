-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
EXECUTE AS USER = 'loader';
UPDATE [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
REVERT;
