ALTER TABLE [sales].[Order] ADD [Status] tinyint NOT NULL CONSTRAINT [DF_Order_Status] DEFAULT (0);
