ALTER TABLE [dbo].[Customer] ADD CONSTRAINT [DF_Customer_Created] DEFAULT (SYSUTCDATETIME()) FOR [CreatedUtc];
