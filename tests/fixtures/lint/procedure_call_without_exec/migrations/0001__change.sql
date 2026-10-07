-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
[sales].[usp_PurgeAll] @confirm = 1;
