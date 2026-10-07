-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow DATA_NO_WHERE [sales].[Staging] reason: staging rows are copies
DELETE FROM [sales].[Staging];
