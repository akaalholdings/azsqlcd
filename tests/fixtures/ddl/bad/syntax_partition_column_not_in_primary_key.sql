-- expect: SYNTAX
-- says: PRIMARY KEY [PK_OrderLine] is unique: the partitioning column [OrderDate] must be one of its key columns
-- line: 5
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderLineId]) ON [PS_OrderYear]([OrderDate])
)
ON [PS_OrderYear]([OrderDate]);
