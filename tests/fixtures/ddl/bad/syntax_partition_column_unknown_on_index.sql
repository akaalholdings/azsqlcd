-- expect: SYNTAX
-- says: the partitioning column [OrderDay] of index [IX_OrderLine] is not a column of the table
-- line: 10
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
);
GO
CREATE NONCLUSTERED INDEX [IX_OrderLine] ON [sales].[OrderLine] ([OrderLineId])
    ON [PS_OrderYear]([OrderDay]);
