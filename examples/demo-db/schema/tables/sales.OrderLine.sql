CREATE TABLE [sales].[OrderLine] (
    [OrderId] bigint NOT NULL,
    [LineNumber] int NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [UnitPrice] decimal(19, 4) NOT NULL,
    [DiscountPercent] decimal(5, 2) NOT NULL CONSTRAINT [DF_OrderLine_DiscountPercent] DEFAULT (0),
    [LineTotal] AS (CONVERT(decimal(19, 4), [Quantity] * [UnitPrice] * (100 - [DiscountPercent]) / 100)) PERSISTED NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderId], [LineNumber]),
    CONSTRAINT [CK_OrderLine_DiscountPercent] CHECK ([DiscountPercent] BETWEEN 0 AND 100),
    CONSTRAINT [CK_OrderLine_Quantity] CHECK ([Quantity] > 0),
    CONSTRAINT [CK_OrderLine_UnitPrice] CHECK ([UnitPrice] >= 0),
    CONSTRAINT [FK_OrderLine_Order] FOREIGN KEY ([OrderId]) REFERENCES [sales].[Order] ([OrderId]) ON DELETE CASCADE,
    CONSTRAINT [FK_OrderLine_Product] FOREIGN KEY ([ProductId]) REFERENCES [product].[Product] ([ProductId])
);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_ProductId] ON [sales].[OrderLine] ([ProductId])
    INCLUDE ([Quantity], [LineTotal]);
