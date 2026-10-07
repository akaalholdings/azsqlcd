CREATE TABLE [sales].[Event] (
    [EventId] bigint NOT NULL,
    [OrderId] int NOT NULL,
    [Kind] varchar(20) NOT NULL,
    [AtUtc] datetime2(3) NOT NULL
);
GO
CREATE CLUSTERED INDEX [CIX_Event] ON [sales].[Event] ([AtUtc], [EventId]);
GO
CREATE NONCLUSTERED INDEX [IX_Event_Kind] ON [sales].[Event] ([Kind], [AtUtc] DESC)
    INCLUDE ([OrderId]);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Event_Id] ON [sales].[Event] ([EventId]);
