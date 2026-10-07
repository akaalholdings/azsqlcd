-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow TRUNCATE [sales].[Staging] reason: staging rows are copies
TRUNCATE TABLE [sales].[Staging];
