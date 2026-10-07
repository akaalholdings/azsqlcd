-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:data
EXEC [sys].[sp_rename] N'[sales].[Order].[Stat]', N'Status', N'COLUMN';
