CREATE TABLE [product].[Product] (
    [ProductId] int IDENTITY(1, 1) NOT NULL,
    [Sku] [product].[Sku] NOT NULL,
    [Name] nvarchar(200) NOT NULL,
    [CategoryId] int NOT NULL,
    [ListPrice] decimal(19, 4) NOT NULL,
    [WeightGrams] int NULL,
    [IsActive] bit NOT NULL CONSTRAINT [DF_Product_IsActive] DEFAULT (1),
    [CreatedUtc] datetime2(3) NOT NULL CONSTRAINT [DF_Product_CreatedUtc] DEFAULT (SYSUTCDATETIME()),
    CONSTRAINT [PK_Product] PRIMARY KEY CLUSTERED ([ProductId]),
    CONSTRAINT [UQ_Product_Sku] UNIQUE NONCLUSTERED ([Sku]),
    CONSTRAINT [CK_Product_ListPrice] CHECK ([ListPrice] >= 0),
    CONSTRAINT [CK_Product_Sku] CHECK ([product].[fn_IsValidSku]([Sku]) = 1),
    CONSTRAINT [CK_Product_WeightGrams] CHECK ([WeightGrams] IS NULL OR [WeightGrams] > 0),
    CONSTRAINT [FK_Product_Category] FOREIGN KEY ([CategoryId]) REFERENCES [product].[Category] ([CategoryId])
);
GO
CREATE NONCLUSTERED INDEX [IX_Product_CategoryId] ON [product].[Product] ([CategoryId])
    INCLUDE ([Name], [ListPrice])
    WHERE [IsActive] = 1;
