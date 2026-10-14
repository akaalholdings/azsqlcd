-- expect: SYNTAX
-- says: the partitioning column
-- line: 4
CREATE NONCLUSTERED INDEX [IX_OrderLine] ON [sales].[OrderLine] ([OrderLineId]) ON [PS_OrderYear]();
