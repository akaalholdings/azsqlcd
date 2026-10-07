ALTER TABLE [sales].[Account] DROP CONSTRAINT [CK_Account_Kind];
GO
ALTER TABLE [sales].[Account] DROP CONSTRAINT [CK_Account_Old];
GO
ALTER TABLE [sales].[Account] DROP CONSTRAINT [DF_Account_Balance];
GO
ALTER TABLE [sales].[Account] DROP CONSTRAINT [DF_Account_ClosedUtc];
GO
ALTER TABLE [sales].[Account] ADD CONSTRAINT [CK_Account_Dates] CHECK ([ClosedUtc] IS NULL OR [ClosedUtc] >= [OpenedUtc]);
GO
ALTER TABLE [sales].[Account] ADD CONSTRAINT [CK_Account_Kind] CHECK ([Kind] IN ('A', 'B', 'C'));
GO
ALTER TABLE [sales].[Account] ADD CONSTRAINT [DF_Account_Balance] DEFAULT ((100)) FOR [Balance];
GO
ALTER TABLE [sales].[Account] ADD CONSTRAINT [DF_Account_Kind] DEFAULT ('A') FOR [Kind];
GO
