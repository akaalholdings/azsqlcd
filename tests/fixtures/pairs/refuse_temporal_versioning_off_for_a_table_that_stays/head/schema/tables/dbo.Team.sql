CREATE TABLE [dbo].[Team] (
    [TeamId] int NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    CONSTRAINT [PK_Team] PRIMARY KEY CLUSTERED ([TeamId])
);
