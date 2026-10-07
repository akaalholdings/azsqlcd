CREATE TABLE [sales].[StaysPacked] (
    [Id] int NOT NULL
);
GO
CREATE CLUSTERED INDEX [CIX_StaysPacked] ON [sales].[StaysPacked] ([Id])
    WITH (DATA_COMPRESSION = ROW);
