CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Note] varchar(50) NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate], [OrderLineId])
);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId]);
