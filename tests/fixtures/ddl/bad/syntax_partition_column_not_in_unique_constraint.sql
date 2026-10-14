-- expect: SYNTAX
-- says: UNIQUE [UQ_OrderLine] is unique: the partitioning column [OrderDate] must be one of its key columns
-- line: 4
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL,
    CONSTRAINT [UQ_OrderLine] UNIQUE NONCLUSTERED ([OrderLineId])
)
ON [PS_OrderYear]([OrderDate]);
