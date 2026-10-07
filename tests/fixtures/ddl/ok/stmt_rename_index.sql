-- azsqlcd:allow RENAME [sales].[Order].[IX_Order_Stat] reason: follows the column rename
EXEC sys.sp_rename N'[sales].[Order].[IX_Order_Stat]', N'IX_Order_Status', N'INDEX';
