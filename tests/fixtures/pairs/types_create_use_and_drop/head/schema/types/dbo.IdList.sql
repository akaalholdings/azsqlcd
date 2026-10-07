CREATE TYPE [dbo].[IdList] AS TABLE (
    [Id] int NOT NULL,
    [Mail] [dbo].[Email] NULL,
    PRIMARY KEY CLUSTERED ([Id])
);
