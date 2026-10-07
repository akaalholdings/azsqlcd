CREATE TABLE [sales].[Customer] (
    [CustomerId] int NOT NULL,
    [Name] nvarchar(100) NOT NULL,
    [Email] varchar(320) NULL,
    [Credit] int NOT NULL,
    [Tier] tinyint NOT NULL CONSTRAINT [DF_Customer_Tier] DEFAULT ((1)),
    CONSTRAINT [PK_Customer] PRIMARY KEY NONCLUSTERED ([CustomerId]),
    CONSTRAINT [UQ_Customer_Mail] UNIQUE NONCLUSTERED ([Email]),
    CONSTRAINT [CK_Customer_Tier] CHECK ([Tier] BETWEEN 1 AND 5)
);
GO
CREATE CLUSTERED INDEX [CIX_Customer_Name] ON [sales].[Customer] ([Name]);
