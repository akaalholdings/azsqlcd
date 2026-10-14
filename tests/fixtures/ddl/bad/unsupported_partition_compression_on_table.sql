-- expect: UNSUPPORTED
-- says: ON PARTITIONS
-- line: 10
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
)
ON [PS_OrderYear]([OrderDate])
WITH (DATA_COMPRESSION = PAGE ON PARTITIONS (1 TO 3));
