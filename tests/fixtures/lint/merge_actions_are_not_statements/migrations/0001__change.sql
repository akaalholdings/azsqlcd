-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
MERGE [sales].[Rate] AS t
USING [sales].[RateLoad] AS s ON t.[Code] = s.[Code]
WHEN MATCHED THEN UPDATE SET t.[Value] = s.[Value]
WHEN NOT MATCHED BY SOURCE THEN DELETE;
