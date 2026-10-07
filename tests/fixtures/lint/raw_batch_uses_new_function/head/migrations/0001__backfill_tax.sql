-- azsqlcd:migration 0001__backfill_tax
-- azsqlcd:mode tx
-- azsqlcd:raw TABLE:[audit].[Log] reason: temporal table, outside the model
-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: reviewed by the DBA team
ALTER TABLE [audit].[Log] ADD [Tax] AS ([sales].[fn_Tax]([Total]));
