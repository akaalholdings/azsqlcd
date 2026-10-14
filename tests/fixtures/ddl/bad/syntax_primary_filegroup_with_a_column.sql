-- expect: SYNTAX
-- says: [PRIMARY] is a filegroup
-- line: 4
CREATE NONCLUSTERED INDEX [IX_OrderLine] ON [sales].[OrderLine] ([OrderLineId]) ON [PRIMARY]([OrderDate]);
