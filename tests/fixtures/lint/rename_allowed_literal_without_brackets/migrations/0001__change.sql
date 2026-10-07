-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: aligned with the API name
EXEC sp_rename 'sales.Order.Stat', 'Status', 'COLUMN';
