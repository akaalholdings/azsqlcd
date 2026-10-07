-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:allow UNMASK [sales].[Customer].[Email] reason: the support role reads the address since r31
ALTER TABLE [sales].[Customer] ALTER COLUMN [Email] DROP MASKED;
GO
ALTER TABLE [sales].[Customer] ALTER COLUMN [Phone] ADD MASKED WITH (FUNCTION = 'partial(0, "XXX", 2)');
GO
ALTER TABLE [sales].[Customer] ADD [Iban] varchar(34) MASKED WITH (FUNCTION = 'default()') NULL;
