-- path: schema/tables/dbo.AuditEvent.sql
CREATE TABLE [dbo].[AuditEvent] (
    [AuditEventId] bigint IDENTITY(1,1) NOT NULL,
    [EventUtc] datetime2(3) NOT NULL CONSTRAINT [DF_AuditEvent_EventUtc] DEFAULT (sysutcdatetime()),
    [EventDate] date NOT NULL CONSTRAINT [DF_AuditEvent_EventDate] DEFAULT (CONVERT(date, DATEADD(day, -1, SYSUTCDATETIME()))),
    [CorrelationId] uniqueidentifier NOT NULL CONSTRAINT [DF_AuditEvent_CorrelationId] DEFAULT NEWSEQUENTIALID(),
    [Actor] sysname NOT NULL CONSTRAINT [DF_AuditEvent_Actor] DEFAULT (SUSER_SNAME()),
    [Severity] tinyint NOT NULL CONSTRAINT [DF_AuditEvent_Severity] DEFAULT ((1)),
    [Amount] decimal(19,4) NOT NULL CONSTRAINT [DF_AuditEvent_Amount] DEFAULT ((0.0)),
    [Payload] nvarchar(max) NULL CONSTRAINT [DF_AuditEvent_Payload] DEFAULT (N'{}'),
    [Source] varchar(30) NOT NULL CONSTRAINT [DF_AuditEvent_Source] DEFAULT ('app'),
    [Bucket] int NOT NULL CONSTRAINT [DF_AuditEvent_Bucket] DEFAULT (ABS(CHECKSUM(NEWID())) % (16)),
    [SeqNo] bigint NOT NULL CONSTRAINT [DF_AuditEvent_SeqNo] DEFAULT (NEXT VALUE FOR [dbo].[AuditSeq])
);
