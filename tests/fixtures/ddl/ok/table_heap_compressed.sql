-- path: schema/tables/dw.Staging.sql
CREATE TABLE [dw].[Staging] (
    [Id] int NOT NULL,
    [Payload] nvarchar(max) NULL
) WITH (DATA_COMPRESSION = PAGE);
GO
CREATE NONCLUSTERED INDEX [IX_Staging_Id] ON [dw].[Staging] ([Id])
    WITH (DATA_COMPRESSION = ROW);
