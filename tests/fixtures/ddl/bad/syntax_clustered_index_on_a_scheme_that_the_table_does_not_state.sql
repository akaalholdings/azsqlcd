-- expect: SYNTAX
-- says: state the same ON clause on CREATE TABLE and on it
-- line: 10
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
);
GO
CREATE CLUSTERED INDEX [CIX_OrderLine] ON [sales].[OrderLine] ([OrderDate])
    ON [PS_OrderYear]([OrderDate]);
