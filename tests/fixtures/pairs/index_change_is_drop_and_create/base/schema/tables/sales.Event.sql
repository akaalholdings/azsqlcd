CREATE TABLE [sales].[Event] (
    [EventId] bigint NOT NULL,
    [OrderId] int NOT NULL,
    [Kind] varchar(20) NOT NULL,
    [AtUtc] datetime2(3) NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_Event_Kind] ON [sales].[Event] ([Kind]);
GO
CREATE NONCLUSTERED INDEX [IX_Event_Old] ON [sales].[Event] ([AtUtc]);
