-- expect: SYNTAX
-- says: a table type cannot be on a partition scheme
-- line: 7
-- path: schema/types/sales.LineList.sql
CREATE TYPE [sales].[LineList] AS TABLE (
    [OrderDate] date NOT NULL,
    PRIMARY KEY CLUSTERED ([OrderDate]) ON [PS_OrderYear]([OrderDate])
);
