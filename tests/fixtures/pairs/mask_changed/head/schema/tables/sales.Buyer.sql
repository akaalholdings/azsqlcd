CREATE TABLE [sales].[Buyer] (
    [BuyerId] int NOT NULL,
    [Mail] nvarchar(320) MASKED WITH (FUNCTION = 'email()') NOT NULL,
    [Phone] varchar(20) MASKED WITH (FUNCTION = 'partial(0, "its ""x""", 2)') NULL,
    [Month] tinyint MASKED WITH (FUNCTION = 'random(1, 12)') NOT NULL,
    [Note] nvarchar(200) NULL,
    CONSTRAINT [PK_Buyer] PRIMARY KEY CLUSTERED ([BuyerId])
);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Buyer_Mail] ON [sales].[Buyer] ([Mail]);
