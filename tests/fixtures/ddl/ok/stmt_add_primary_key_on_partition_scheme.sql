ALTER TABLE [sales].[OrderLine] ADD CONSTRAINT [PK_OrderLine] PRIMARY KEY CLUSTERED ([OrderDate], [OrderLineId]) WITH (DATA_COMPRESSION = PAGE, ONLINE = ON) ON [PS_OrderYear]([OrderDate]);
