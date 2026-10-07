CREATE TABLE [sales].[Address] (
    [AddressId] int IDENTITY(1, 1) NOT NULL,
    [CustomerId] int NOT NULL,
    [Kind] char(1) NOT NULL CONSTRAINT [DF_Address_Kind] DEFAULT ('S'),
    [Line1] nvarchar(200) NOT NULL,
    [Line2] nvarchar(200) NULL,
    [City] nvarchar(100) NOT NULL,
    [PostalCode] nvarchar(20) NOT NULL,
    [CountryCode] char(2) NOT NULL,
    CONSTRAINT [PK_Address] PRIMARY KEY CLUSTERED ([AddressId]),
    CONSTRAINT [CK_Address_Kind] CHECK ([Kind] IN ('B', 'S')),
    CONSTRAINT [FK_Address_Customer] FOREIGN KEY ([CustomerId]) REFERENCES [sales].[Customer] ([CustomerId]) ON DELETE CASCADE
);
GO
CREATE NONCLUSTERED INDEX [IX_Address_CustomerId] ON [sales].[Address] ([CustomerId]);
