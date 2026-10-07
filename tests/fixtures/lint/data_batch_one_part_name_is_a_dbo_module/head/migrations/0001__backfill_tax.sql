-- azsqlcd:migration 0001__backfill_tax
-- azsqlcd:mode tx
-- azsqlcd:data
UPDATE [sales].[Order]
SET [Tax] = sales.FN_TAX([Total])
WHERE [Tax] IS NULL;
GO
-- azsqlcd:data
UPDATE [sales].[Order]
SET [Total] = fn_Round([Total])
WHERE [Total] IS NOT NULL;
