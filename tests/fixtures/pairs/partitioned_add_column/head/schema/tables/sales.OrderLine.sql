CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Note] varchar(50) NULL,
    [Discount] decimal(5, 2) NOT NULL CONSTRAINT [DF_OrderLine_Discount] DEFAULT ((0)),
    CONSTRAINT [PK_OrderLine] PRIMARY KEY NONCLUSTERED ([OrderLineId]) ON [PRIMARY],
    CONSTRAINT [CK_OrderLine_Quantity] CHECK ([Quantity] > 0)
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate], [OrderLineId])
    ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Product] ON [sales].[OrderLine] ([ProductId])
    ON [PS_OrderYear]([OrderDate]);
