-- path: schema/tables/dbo.Reserved.sql
CREATE TABLE [dbo].[Reserved] (
    [Index] int NOT NULL,
    [Order] int NOT NULL,
    [User] sysname NOT NULL,
    [Key] varchar(10) NOT NULL,
    [Default] int NULL,
    [Constraint] int NULL,
    Period int NOT NULL,
    [Primary] bit NOT NULL,
    CONSTRAINT [PK_Reserved] PRIMARY KEY CLUSTERED ([Index], [Order] DESC),
    CONSTRAINT [CK_Reserved_Key] CHECK ([Key] <> 'key' AND [Default] IS NULL)
);
