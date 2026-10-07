CREATE TABLE [sales].[Job] (
    [JobId] int NOT NULL,
    [State] tinyint NOT NULL,
    [DoneUtc] datetime2(3) NULL,
    CONSTRAINT [PK_Job] PRIMARY KEY CLUSTERED ([JobId]) WITH (FILLFACTOR = 100)
);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Job_Open] ON [sales].[Job] ([State])
    WHERE [DoneUtc] IS NULL
    WITH (FILLFACTOR = 90);
