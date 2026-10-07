-- azsqlcd:allow UNMASK [sales].[Buyer].[Phone] reason: the support team needs the number
ALTER TABLE [sales].[Buyer] ALTER COLUMN [Phone] DROP MASKED;
