CREATE TABLE [dbo].[Contact] (
    [ContactId] int NOT NULL,
    [Phone] [dbo].[Phone] NULL,
    [Email] varchar(200) NULL,
    CONSTRAINT [PK_Contact] PRIMARY KEY CLUSTERED ([ContactId])
);
