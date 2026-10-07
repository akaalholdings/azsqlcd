CREATE TABLE [sales].[Customer] (
    [CustomerId] int IDENTITY(1, 1) NOT NULL,
    [Email] nvarchar(320) NOT NULL,
    [DisplayName] nvarchar(200) NOT NULL,
    [CountryCode] char(2) NOT NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Customer_IsActive] DEFAULT (1),
    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Customer_CreatedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_Customer] PRIMARY KEY CLUSTERED ([CustomerId]),
    CONSTRAINT [CK_Customer_CountryCode] CHECK ([CountryCode] LIKE '[A-Z][A-Z]'),
    CONSTRAINT [CK_Customer_Email] CHECK ([Email] LIKE '_%@_%')
);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Customer_Email] ON [sales].[Customer] ([Email]);
