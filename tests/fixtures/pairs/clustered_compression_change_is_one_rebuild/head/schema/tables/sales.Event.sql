CREATE TABLE [sales].[Event] (
    [EventId] bigint NOT NULL,
    [At] datetime2(3) NOT NULL
);
GO
CREATE CLUSTERED INDEX [CIX_Event] ON [sales].[Event] ([At])
    WITH (DATA_COMPRESSION = PAGE);
