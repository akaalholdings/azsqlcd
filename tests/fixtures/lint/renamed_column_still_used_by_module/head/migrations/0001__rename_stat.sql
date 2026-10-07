-- azsqlcd:migration 0001__rename_stat
-- azsqlcd:mode tx
-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: aligned with the API name
EXEC sys.sp_rename N'[sales].[Order].[Stat]', N'Status', N'COLUMN';
