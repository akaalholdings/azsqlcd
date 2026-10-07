CREATE TABLE [sales].[Buyer] (
    [BuyerId] int NOT NULL,
    [Mail] nvarchar(320) MASKED WITH (FUNCTION = 'email()') NOT NULL,
    [Phone] varchar(20) NULL,
    [Month] tinyint NOT NULL,
    [Note] nvarchar(200) NULL,
    CONSTRAINT [PK_Buyer] PRIMARY KEY CLUSTERED ([BuyerId])
);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_Buyer_Mail] ON [sales].[Buyer] ([Mail]);
