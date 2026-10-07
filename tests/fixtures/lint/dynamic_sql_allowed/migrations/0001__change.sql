-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow DYNAMIC_SQL batch reason: the day comes from a parameter, not from text
EXECUTE sys.sp_executesql N'DELETE FROM [sales].[Staging] WHERE [Day] < @d', N'@d int', @d = 10;
