-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
-- azsqlcd:allow OLD_MODULE [sales].[fn_Tax] reason: the old rate is right for these rows
UPDATE [sales].[Order] SET [Tax] = [sales].[fn_Tax]([Total]) WHERE [Tax] IS NULL;
