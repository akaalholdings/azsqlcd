-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:raw TABLE:[audit].[Log] reason: temporal table, outside the model
-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: reviewed by the DBA team, runs in 1 second
ALTER TABLE [audit].[Log] SET (SYSTEM_VERSIONING = OFF);
GRANT SELECT ON [audit].[Log] TO [audit_reader];
