ALTER TABLE [sales].[Coupon] ALTER COLUMN [Code] varchar(40) NOT NULL;
GO
ALTER TABLE [sales].[Coupon] ALTER COLUMN [Tag] nvarchar(10) NULL;
GO
ALTER TABLE [sales].[Coupon] ALTER COLUMN [Hash] varbinary(32) NOT NULL;
GO
