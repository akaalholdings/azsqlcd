-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow EXEC_PROC [sales].[usp_RebuildTotals] reason: idempotent, 3 seconds in test
DECLARE @rc int;
EXEC @rc = sales.usp_RebuildTotals @Year = 2026;
