-- azsqlcd:migration 0001__backfill_tax
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow OLD_MODULE [sales].[fn_Tax] reason: these rows keep the old rate
UPDATE [sales].[Order]
SET [Tax] = sales.FN_TAX([Total])
WHERE [Tax] IS NULL;
