ALTER TABLE [dbo].[Staff] ADD [Phone] nvarchar(20) NULL;
GO
ALTER TABLE [dbo].[Staff] ADD [Grade] tinyint NOT NULL CONSTRAINT [DF_Staff_Grade] DEFAULT ((1));
GO
CREATE NONCLUSTERED INDEX [IX_Staff_Phone] ON [dbo].[Staff] ([Phone]) WHERE [Phone] IS NOT NULL;
GO
ALTER TABLE [dbo].[Staff] ADD CONSTRAINT [CK_Staff_Grade] CHECK ([Grade] BETWEEN 1 AND 9);
GO
