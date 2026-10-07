ALTER TABLE [sales].[Line] DROP CONSTRAINT [FK_Line_Parent];
GO
ALTER TABLE [sales].[Line] DROP CONSTRAINT [CK_Line_Qty];
GO
ALTER TABLE [sales].[Line] ADD CONSTRAINT [CK_Line_Qty] CHECK NOT FOR REPLICATION ([Qty] > 0);
GO
ALTER TABLE [sales].[Line] ADD CONSTRAINT [FK_Line_Parent] FOREIGN KEY ([ParentId]) REFERENCES [sales].[Line] ([LineId]);
GO
