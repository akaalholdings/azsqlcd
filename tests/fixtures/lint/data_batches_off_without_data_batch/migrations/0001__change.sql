-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
ALTER TABLE [sales].[Order] ADD [Status] int NULL;
GO
-- azsqlcd:raw TABLE:[audit].[Log] reason: outside the model
-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: agreed with the owner
ALTER TABLE [audit].[Log] REBUILD;
