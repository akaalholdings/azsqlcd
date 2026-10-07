-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:raw TABLE:[audit].[Log] reason: temporal table, outside the model
ALTER TABLE [audit].[Log] SET (SYSTEM_VERSIONING = OFF);
