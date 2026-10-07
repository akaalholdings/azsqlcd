CREATE TABLE [sales].[Person] (
    [PersonId] int IDENTITY(1, 1) NOT NULL,
    [OldGuid] uniqueidentifier ROWGUIDCOL NOT NULL,
    [NewGuid] uniqueidentifier NOT NULL,
    [Email] varchar(320) MASKED WITH (FUNCTION = 'default()') NULL,
    [Phone] varchar(20) NULL,
    [Card] char(16) MASKED WITH (FUNCTION = 'partial(0, "XXXXXXXXXXXX", 4)') NULL,
    [Notes] nvarchar(400) SPARSE NULL,
    [Extra] nvarchar(400) NULL,
    [Wide] varchar(10) SPARSE NULL,
    CONSTRAINT [PK_Person] PRIMARY KEY CLUSTERED ([PersonId])
);
