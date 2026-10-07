-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow EXEC_PROC [sales].[usp_PurgeAll] reason: the purge was agreed for r41
[sales].[usp_PurgeAll] @confirm = 1;
