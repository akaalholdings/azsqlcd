EXEC sys.sp_rename N'[sales].[Account].[idx1]', N'IX_Account_Opened', N'INDEX';
GO
EXEC sys.sp_rename N'[sales].[PK__Account__3214EC07]', N'PK_Account', N'OBJECT';
GO
EXEC sys.sp_rename N'[sales].[DF__Account__Balance]', N'DF_Account_Balance', N'OBJECT';
GO
EXEC sys.sp_rename N'[sales].[CK__Account__1A2B]', N'CK_Account_Old', N'OBJECT';
GO
