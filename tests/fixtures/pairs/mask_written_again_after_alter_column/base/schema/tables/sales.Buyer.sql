CREATE TABLE [sales].[Buyer] (
    [BuyerId] int NOT NULL,
    [Mail] nvarchar(100) MASKED WITH (FUNCTION = 'email()') NOT NULL,
    [Phone] varchar(20) MASKED WITH (FUNCTION = 'default()') NULL,
    [Month] tinyint MASKED WITH (FUNCTION = 'random(1, 12)') NOT NULL,
    [Note] nvarchar(200) NULL,
    CONSTRAINT [PK_Buyer] PRIMARY KEY CLUSTERED ([BuyerId])
);
