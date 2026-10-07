DROP INDEX [IX_Line_Total] ON [sales].[Line];
GO
ALTER TABLE [sales].[Line] DROP COLUMN [Total];
GO
ALTER TABLE [sales].[Line] ADD [Gross] AS ([Qty] * [Price] * 1.2);
GO
