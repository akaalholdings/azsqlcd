-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE TOP (1000) [sales].[Order] SET [Status] = 1 WHERE [Status] IS NULL;
GO 50
