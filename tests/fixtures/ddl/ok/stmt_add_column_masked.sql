ALTER TABLE [sales].[Buyer] ADD [TaxNumber] varchar(20) MASKED WITH (FUNCTION = 'default()') NULL;
