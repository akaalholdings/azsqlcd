-- path: schema/tables/dbo.Session.sql
CREATE TABLE [dbo].[Session] (
    [SessionId] uniqueidentifier NOT NULL CONSTRAINT [PK_Session] PRIMARY KEY NONCLUSTERED,
    [UserId] int NOT NULL INDEX [IX_Session_UserId] NONCLUSTERED,
    [StartedUtc] datetime2(3) NOT NULL,
    [EndedUtc] datetime2(3) NULL,
    [IpAddress] varchar(45) NULL,
    INDEX [CIX_Session_Started] CLUSTERED ([StartedUtc] DESC, [SessionId]),
    INDEX [IX_Session_Open] NONCLUSTERED ([UserId], [StartedUtc]) INCLUDE ([IpAddress]) WHERE [EndedUtc] IS NULL WITH (FILLFACTOR = 90, DATA_COMPRESSION = ROW),
    INDEX [UX_Session_Ip] UNIQUE NONCLUSTERED ([IpAddress], [StartedUtc])
);
