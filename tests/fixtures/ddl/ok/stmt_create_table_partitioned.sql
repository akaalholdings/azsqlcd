CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Quantity] int NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate]),
    CONSTRAINT [UQ_OrderLine] UNIQUE NONCLUSTERED ([OrderLineId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderDate]);
