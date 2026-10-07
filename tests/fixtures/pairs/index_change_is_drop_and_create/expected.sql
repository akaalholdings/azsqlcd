DROP INDEX [IX_Event_Kind] ON [sales].[Event];
GO
DROP INDEX [IX_Event_Old] ON [sales].[Event];
GO
CREATE CLUSTERED INDEX [CIX_Event] ON [sales].[Event] ([AtUtc], [EventId]);
GO
CREATE NONCLUSTERED INDEX [IX_Event_Kind] ON [sales].[Event] ([Kind], [AtUtc] DESC) INCLUDE ([OrderId]);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Event_Id] ON [sales].[Event] ([EventId]);
GO
