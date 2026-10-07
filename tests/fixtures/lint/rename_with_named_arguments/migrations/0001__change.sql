-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow RENAME [sales].[Order].[Stat] reason: aligned with the API name
EXEC sys.sp_rename @objname = N'[sales].[Order].[Stat]', @newname = N'Status', @objtype = N'COLUMN';
