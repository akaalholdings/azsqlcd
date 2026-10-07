CREATE TABLE [sales].[Product] (
    [ProductId] int NOT NULL,
    [Name] nvarchar(100) NULL,
    [Price] decimal(10, 2) NOT NULL CONSTRAINT [DF_Product_Price] DEFAULT ((0)),
    [Code] char(8) NOT NULL,
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId])
);
