-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
DECLARE @sql nvarchar(max) = N'DELETE FROM [sales].[Staging] WHERE [Day] < 10';
EXEC (@sql);
