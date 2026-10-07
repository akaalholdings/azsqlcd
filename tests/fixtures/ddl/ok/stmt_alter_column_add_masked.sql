ALTER TABLE [sales].[Buyer] ALTER COLUMN [Phone] ADD MASKED WITH (FUNCTION = 'partial(1, "XXXX", 0)');
