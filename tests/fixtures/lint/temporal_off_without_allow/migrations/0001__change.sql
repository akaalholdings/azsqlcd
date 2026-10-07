-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Price] SET (SYSTEM_VERSIONING = OFF);
