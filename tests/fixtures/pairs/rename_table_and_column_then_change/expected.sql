EXEC sys.sp_rename N'[sales].[Ordr]', N'Order', N'OBJECT';
GO
EXEC sys.sp_rename N'[sales].[Order].[Note]', N'Remark', N'COLUMN';
GO
ALTER TABLE [sales].[Order] ADD [ClosedUtc] datetime2(3) NULL;
GO
ALTER TABLE [sales].[Order] ALTER COLUMN [Remark] nvarchar(max) NULL;
GO
CREATE NONCLUSTERED INDEX [IX_Order_Stat] ON [sales].[Order] ([Stat]) INCLUDE ([CustomerId]);
GO
