-- expect: SYNTAX
-- says: the partitioning column [OrderDay] of the table is not a column of the table
-- line: 5
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
)
ON [PS_OrderYear]([OrderDay]);
