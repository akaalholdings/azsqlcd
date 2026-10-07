ALTER TABLE [sales].[Product] ALTER COLUMN [Name] nvarchar(400) NOT NULL;
GO
ALTER TABLE [sales].[Product] ALTER COLUMN [Price] decimal(12, 2) NOT NULL;
GO
ALTER TABLE [sales].[Product] ALTER COLUMN [Code] varchar(20) NULL;
GO
