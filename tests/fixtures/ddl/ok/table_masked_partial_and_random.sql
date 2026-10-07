-- path: schema/tables/sales.Buyer.sql
CREATE TABLE [sales].[Buyer] (
    [BuyerId] int MASKED WITH (FUNCTION = 'default()') IDENTITY(1, 1) NOT NULL,
    [Phone] varchar(20) COLLATE Latin1_General_100_BIN2 MASKED WITH (FUNCTION = 'partial(1, "XXXX", 0)') NOT NULL,
    [Card] nvarchar(30) MASKED WITH (FUNCTION = 'partial(0, "it''s ""hidden"", (x)", 4)') NULL,
    [Month] tinyint MASKED WITH (FUNCTION = 'random(1, 12)') NOT NULL CONSTRAINT [DF_Buyer_Month] DEFAULT ((1)),
    [Balance] decimal(9, 2) MASKED WITH (FUNCTION = 'random(1.00, 12.50)') NULL,
    [Score] float MASKED WITH (FUNCTION = 'random(-1.5, 12)') NULL,
    [Seen] datetime2(3) MASKED WITH (FUNCTION = 'datetime("Y")') NULL,
    [Plain] nvarchar(10) NULL,
    CONSTRAINT [PK_Buyer] PRIMARY KEY CLUSTERED ([BuyerId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Buyer_Phone] ON [sales].[Buyer] ([Phone]);
