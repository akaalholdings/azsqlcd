-- path: schema/tables/sales.OrderTotal.sql
CREATE TABLE [sales].[OrderTotal] (
    [OrderDate] date NOT NULL,
    [OrderId] int NOT NULL,
    [Total] money NOT NULL,
    CONSTRAINT [PK_OrderTotal] PRIMARY KEY CLUSTERED ([OrderDate], [OrderId]) WITH (STATISTICS_INCREMENTAL = ON) ON [PS_OrderYear]([OrderDate])
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE NONCLUSTERED INDEX [IX_OrderTotal_Total] ON [sales].[OrderTotal] ([Total])
    WITH (STATISTICS_INCREMENTAL = OFF, STATISTICS_NORECOMPUTE = ON)
    ON [PS_OrderYear]([OrderDate]);
