-- expect: SYNTAX
-- says: index [CIX_OrderLine] is clustered: it is the table and has the place of the table
-- line: 11
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate])
    ON [PRIMARY];
