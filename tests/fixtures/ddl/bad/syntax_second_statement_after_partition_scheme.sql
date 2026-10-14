-- expect: SYNTAX
-- says: expected the end of the statement
-- line: 5
CREATE NONCLUSTERED INDEX [IX_OrderLine] ON [sales].[OrderLine] ([OrderLineId]) ON [PS_OrderYear]([OrderDate])
DROP TABLE [sales].[OrderLine];
