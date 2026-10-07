CREATE TABLE [sales].[WasClustered] (
    [Id] int NOT NULL,
    CONSTRAINT [PK_WasClustered] PRIMARY KEY CLUSTERED ([Id]) WITH (DATA_COMPRESSION = PAGE)
);
