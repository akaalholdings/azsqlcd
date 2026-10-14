-- expect: SYNTAX
-- says: index [UX_OrderLine_Line] is unique: the partitioning column [OrderDate] must be one of its key columns
-- line: 11
-- path: schema/tables/sales.OrderLine.sql
CREATE TABLE [sales].[OrderLine] (
    [OrderLineId] bigint NOT NULL,
    [OrderDate] date NOT NULL
)
ON [PS_OrderYear]([OrderDate]);
GO
CREATE UNIQUE NONCLUSTERED INDEX [UX_OrderLine_Line] ON [sales].[OrderLine] ([OrderLineId])
    INCLUDE ([OrderDate])
    ON [PS_OrderYear]([OrderDate]);
