DROP TABLE [sales].[OrderLineStage];
GO
CREATE TABLE [sales].[FactOrderLine] (
    [OrderDate] date NOT NULL,
    [ProductKey] int NOT NULL,
    [Amount] decimal(19, 4) NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE TABLE [sales].[OrderEvent] (
    [OrderEventId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [Kind] tinyint NOT NULL,
    CONSTRAINT [PK_OrderEvent] PRIMARY KEY CLUSTERED ([OrderDate], [OrderEventId]) WITH (STATISTICS_INCREMENTAL = ON) ON [PS_OrderYear]([OrderDate]),
    CONSTRAINT [UQ_OrderEvent] UNIQUE NONCLUSTERED ([OrderEventId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    [ProductId] int NOT NULL,
    [Quantity] int NOT NULL,
    [Note] varchar(50) NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY NONCLUSTERED ([OrderLineId]) ON [PRIMARY]
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED COLUMNSTORE INDEX [CCI_FactOrderLine] ON [sales].[FactOrderLine] WITH (DATA_COMPRESSION = COLUMNSTORE_ARCHIVE) ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_FactOrderLine_Product] ON [sales].[FactOrderLine] ([ProductKey]) ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderEvent_Kind] ON [sales].[OrderEvent] ([Kind]) ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate], [OrderLineId]) ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine_Quantity] ON [sales].[OrderLine] ([Quantity]) ON [PRIMARY];
GO
