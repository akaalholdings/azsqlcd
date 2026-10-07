ALTER TABLE [dbo].[Customer] ADD [IsActive] bit NULL CONSTRAINT [DF_Customer_IsActive] DEFAULT (1) WITH VALUES;
