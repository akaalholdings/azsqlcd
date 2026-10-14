-- expect: SYNTAX
-- says: a known option
-- line: 4
CREATE CLUSTERED COLUMNSTORE INDEX [CCI] ON [sales].[OrderLine] WITH (STATISTICS_INCREMENTAL = ON) ON [PS_OrderYear]([OrderDate]);
