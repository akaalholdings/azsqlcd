ALTER TABLE [sales].[Buyer] ALTER COLUMN [Mail] ADD MASKED WITH (FUNCTION = 'email()');
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Phone] ADD MASKED WITH (FUNCTION = 'partial(1, "XXXX", 0)');
GO
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Month] ADD MASKED WITH (FUNCTION = 'random(1, 12)');
GO
