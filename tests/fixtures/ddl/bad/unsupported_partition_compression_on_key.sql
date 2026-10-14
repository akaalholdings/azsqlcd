-- expect: UNSUPPORTED
-- says: ON PARTITIONS
-- line: 4
ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate]) WITH (DATA_COMPRESSION = ROW ON PARTITIONS (2)) ON [PS_OrderYear]([OrderDate]);
