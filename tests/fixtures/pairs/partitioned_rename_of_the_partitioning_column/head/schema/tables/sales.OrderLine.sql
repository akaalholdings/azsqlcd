CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderedOn] date NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Note] varchar(50) NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY NONCLUSTERED ([OrderLineId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderedOn]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderedOn], [OrderLineId])
    ON [PS_OrderYear]([OrderedOn]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity])
    ON [PRIMARY];
