CREATE TABLE [sales].[Buyer] (
    [BuyerId] int NOT NULL,
    [Mail] nvarchar(320) MASKED WITH (FUNCTION = 'email()') NOT NULL,
    [Phone] varchar(40) NOT NULL,
    [Month] int MASKED WITH (FUNCTION = 'random(1, 31)') NOT NULL,
    [Note] nvarchar(200) MASKED WITH (FUNCTION = 'default()') NOT NULL,
    [TaxNumber] varchar(20) MASKED WITH (FUNCTION = 'default()') NULL,
    CONSTRAINT [PK_Buyer] PRIMARY KEY CLUSTERED ([BuyerId])
);
