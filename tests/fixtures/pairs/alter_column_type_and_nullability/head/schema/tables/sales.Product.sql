CREATE TABLE [sales].[Product] (
    [ProductId] int NOT NULL,
    [Name] nvarchar(400) NOT NULL,
    [Price] decimal(12, 2) NOT NULL CONSTRAINT [DF_Product_Price] DEFAULT ((0)),
    [Code] varchar(20) NULL,
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId])
);
