ALTER TABLE [sales].[Buyer] ADD [TaxNumber] varchar(20) MASKED WITH (FUNCTION = 'default()') NULL;
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] nvarchar(320) NOT NULL;
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] ADD MASKED WITH (FUNCTION = 'email()');
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Phone] DROP MASKED;
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Phone] varchar(40) NOT NULL;
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Month] int NOT NULL;
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Month] ADD MASKED WITH (FUNCTION = 'random(1, 31)');
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Note] nvarchar(200) NOT NULL;
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Note] ADD MASKED WITH (FUNCTION = 'default()');
GO
