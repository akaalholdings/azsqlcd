CREATE TYPE [dbo].[Email] FROM varchar(200) NULL;
GO
CREATE TYPE [dbo].[IdList] AS TABLE (
    [Id] int NOT NULL,
    [Mail] [dbo].[Email] NULL,
    PRIMARY KEY CLUSTERED ([Id])
);
GO
ALTER TABLE [dbo].[Contact] ALTER COLUMN [Phone] varchar(20) NULL;
GO
ALTER TABLE [dbo].[Contact] ALTER COLUMN [Email] [dbo].[Email] NULL;
GO
DROP TYPE [dbo].[Phone];
GO
