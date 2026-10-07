-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
EXEC [sales].[usp_RebuildTotals] @Year = 2026;
