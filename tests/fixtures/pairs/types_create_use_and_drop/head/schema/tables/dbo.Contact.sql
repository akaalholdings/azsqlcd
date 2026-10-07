CREATE TABLE [dbo].[Contact] (
    [ContactId] int NOT NULL,
    [Phone] varchar(20) NULL,
    [Email] [dbo].[Email] NULL,
    CONSTRAINT [PK_Contact] PRIMARY KEY CLUSTERED ([ContactId])
);
