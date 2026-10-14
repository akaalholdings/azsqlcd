-- expect: SYNTAX
-- says: a partition scheme takes one partitioning column
-- line: 9
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
)
ON [PS_OrderYear]([OrderDate], [OrderLineId]);
