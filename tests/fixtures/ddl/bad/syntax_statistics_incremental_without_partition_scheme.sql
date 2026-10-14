-- expect: SYNTAX
-- says: STATISTICS_INCREMENTAL goes with an index on a partition scheme
-- line: 4
CREATE NONCLUSTERED INDEX [IX_OrderLine] ON [sales].[OrderLine] ([OrderLineId]) WITH (STATISTICS_INCREMENTAL = ON);
