-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
EXECUTE sys.sp_executesql N'DELETE FROM [sales].[Staging] WHERE [Day] < @d', N'@d int', @d = 10;
