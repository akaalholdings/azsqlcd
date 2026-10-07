CREATE TABLE [sales].[Person] (
    [PersonId] int IDENTITY(1, 1) NOT FOR REPLICATION NOT NULL,
    [OldGuid] uniqueidentifier NOT NULL,
    [NewGuid] uniqueidentifier ROWGUIDCOL NOT NULL,
    [Email] varchar(320) MASKED WITH (FUNCTION = 'email()') NULL,
    [Phone] varchar(20) MASKED WITH (FUNCTION = 'partial(0, "XXX-XXX-", 4)') NULL,
    [Card] char(16) NULL,
    [Notes] nvarchar(400) NULL,
    [Extra] nvarchar(400) SPARSE NULL,
    [Wide] varchar(50) SPARSE NULL,
    [Added] varchar(100) MASKED WITH (FUNCTION = 'default()') NULL,
    CONSTRAINT [PK_Person] PRIMARY KEY CLUSTERED ([PersonId])
);
