ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] ADD MASKED WITH (FUNCTION = 'email()');
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Phone] ADD MASKED WITH (FUNCTION = 'partial(0, "its ""x""", 2)');
GO
