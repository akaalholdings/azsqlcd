-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:raw TABLE:[audit].[Log] reason: a memory-optimised table, outside the model
-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: reviewed by the DBA team, the table is new
CREATE TABLE [audit].[Log] (
    [Id] int NOT NULL,
    [Amount] numeric(23, 5) NULL
    [Note])
)
    WITH (MEMORY_OPTIMIZED = ON);
