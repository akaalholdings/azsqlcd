-- azsqlcd:migration 0001__change
-- azsqlcd:mode tx
-- azsqlcd:raw TABLE:[audit].[Log] reason: a memory-optimised table, outside the model
-- azsqlcd:allow RAW TABLE:[audit].[Log] reason: reviewed by the DBA team, the table is new
CREATE TABLE [audit].[Log] (
    [Id] int NOT NULL, -- the key (no identity
    [Note )] nvarchar(40) NOT NULL CONSTRAINT [DF_Log_Note] DEFAULT (N'(none'),
    "Size (" numeric(23, 5) NULL /* ((precision, scale) */
)
    WITH (MEMORY_OPTIMIZED = ON);
