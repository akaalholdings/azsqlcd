-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- the azsqlcd:allow line below covers the drop
-- azsqlcd:allow DROP_COLUMN [sales].[Order].[LegacyCode] reason: replaced by Code in r41
/* --azsqlcd:allow in a block comment is text */
ALTER TABLE [sales].[Order] DROP COLUMN [LegacyCode]; -- see --azsqlcd:allow in the runbook
