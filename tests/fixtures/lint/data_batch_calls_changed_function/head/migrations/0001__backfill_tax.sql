-- azsqlcd:migration 0001__backfill_tax
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE [sales].[Order]
SET [Tax] = sales.FN_TAX([Total])
WHERE [Tax] IS NULL;
